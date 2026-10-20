"""Render an SVG to PNG with Qt's SVG renderer, which ships with PyQt6.

(CairoSVG was the first renderer, but it needs the native cairo library, which pip can't
install and which Python doesn't find in /opt/homebrew/lib even after `brew install cairo`.)

Qt refuses a path with more than roughly 32,000 commands ("Invalid path data; path
truncated") and draws nothing for it, and busy artwork such as cross-hatching produces paths
far longer than that. Those documents are drawn by a small polygon rasterizer instead, which
handles exactly what finalize writes: absolute M/L/Z paths, circles and rounded rectangles.
"""
import io
import os
import re

import numpy as np
from PIL import Image, ImageColor, ImageDraw


class PreviewUnavailable(RuntimeError):
    pass


_app = None   # an offscreen QGuiApplication, created on first use when no app exists
QT_PATH_LIMIT = 30_000   # commands per path; Qt gives up somewhere between 30,000 and 40,000


def render(svg_text, width, height, *, background=None):
    """PNG bytes of the SVG document SVG_TEXT drawn into WIDTH x HEIGHT, aspect ratio kept and
    centered. background=None means transparent."""
    text = svg_text.decode("utf-8") if isinstance(svg_text, (bytes, bytearray)) else svg_text
    if any(len(re.findall(r"[A-Za-z]", d)) > QT_PATH_LIMIT for d in re.findall(r' d="([^"]*)"', text)):
        png = _render_polygons(text, width, height, background)
        if png is not None:
            return png
    return _render_qt(text, width, height, background)


def _render_qt(text, width, height, background):
    try:
        from PyQt6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
        from PyQt6.QtGui import QColor, QGuiApplication, QImage, QPainter
        from PyQt6.QtSvg import QSvgRenderer
    except ImportError as e:
        raise PreviewUnavailable(f"PyQt6 with QtSvg is needed for previews ({e})") from e

    global _app
    if QGuiApplication.instance() is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        _app = QGuiApplication(["marquetry"])
    r = QSvgRenderer(QByteArray(text.encode("utf-8")))
    if not r.isValid():
        raise PreviewUnavailable("Qt could not read the SVG")
    r.setAspectRatioMode(Qt.AspectRatioMode.KeepAspectRatio)
    img = QImage(width, height, QImage.Format.Format_ARGB32)
    img.fill(QColor(background) if background else QColor(0, 0, 0, 0))
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    r.render(p, QRectF(0, 0, width, height))
    p.end()
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    if not img.save(buf, "PNG"):
        raise PreviewUnavailable("could not encode the preview")
    buf.close()
    return bytes(data)


def _attrs(tag):
    return dict(re.findall(r'([\w-]+)="([^"]*)"', tag))


def _render_polygons(text, width, height, background):
    """Rasterize finalize's output (absolute M/L/Z paths, circle, rect) with PIL, filling each
    path even-odd: its subpaths are disjoint outlines and the holes inside them, for which
    even-odd and nonzero agree. Returns None for anything else, which then goes to Qt."""
    svg = re.search(r"<svg\b[^>]*>", text)
    if not svg:
        return None
    a = _attrs(svg.group(0))
    try:
        W, H = float(a["width"]), float(a["height"])
    except (KeyError, ValueError):
        return None
    s = min(width / W, height / H)
    ox, oy = (width - W * s) / 2, (height - H * s) / 2
    img = Image.new("RGBA", (width, height), ImageColor.getrgb(background) + (255,) if background
                    else (0, 0, 0, 0))
    for tag in re.findall(r"<(?:path|circle|rect)\b[^>]*>", text):
        t = _attrs(tag)
        if "transform" in t:
            return None
        try:
            fill = ImageColor.getrgb(t.get("fill", "#000000")) + (255,)
        except ValueError:
            return None
        mask = Image.new("1", (width, height), 0)
        d = ImageDraw.Draw(mask)
        if tag.startswith("<path"):
            path = t.get("d", "")
            if re.search(r"[^MLZ0-9.,\s-]", path):
                return None
            acc = np.zeros((height, width), bool)
            for sub in re.split(r"(?=M)", path):
                nums = [float(v) for v in re.findall(r"-?\d*\.?\d+", sub)]
                xs = np.asarray(nums[0::2]) * s + ox
                ys = np.asarray(nums[1::2]) * s + oy
                if min(len(xs), len(ys)) < 3:
                    continue
                # draw each outline in its own bounding box and XOR it in (even-odd)
                x0, y0 = max(int(xs.min()) - 1, 0), max(int(ys.min()) - 1, 0)
                x1, y1 = min(int(xs.max()) + 2, width), min(int(ys.max()) + 2, height)
                if x1 <= x0 or y1 <= y0:
                    continue
                layer = Image.new("1", (x1 - x0, y1 - y0), 0)
                ImageDraw.Draw(layer).polygon(list(zip(xs - x0, ys - y0)), fill=1)
                acc[y0:y1, x0:x1] ^= np.asarray(layer)
            mask = Image.fromarray(acc)
        elif tag.startswith("<circle"):
            cx, cy, r = (float(t[k]) for k in ("cx", "cy", "r"))
            d.ellipse((ox + (cx - r) * s, oy + (cy - r) * s, ox + (cx + r) * s, oy + (cy + r) * s), fill=1)
        else:
            x, y, w, h = (float(t.get(k, 0)) for k in ("x", "y", "width", "height"))
            d.rounded_rectangle((ox + x * s, oy + y * s, ox + (x + w) * s, oy + (y + h) * s),
                                radius=float(t.get("rx", 0)) * s, fill=1)
        img.paste(Image.new("RGBA", (width, height), fill), (0, 0), mask)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
