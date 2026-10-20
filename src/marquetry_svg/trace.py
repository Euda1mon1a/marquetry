"""Step 2: trace the posterized image with vtracer, one color at a time.

Each palette color is traced from its own layer: that color opaque, everything else
transparent. The colors come out disjoint (a color on top of another is a hole in the one
below, so parts don't overlap in the slicer beyond the ~1.5 px where two traced outlines of a
shared curved or diagonal edge disagree), and vtracer's clustering never gets to merge a small
detail of one color (an eye, the dot of an i) into the color around it, which its multi-color
cutout mode does to anything under about 10 x 10 pixels.

Each layer is traced in vtracer's "stacked" mode. On a one-color layer it keeps holes just
like "cutout", and its cost grows with the image instead of superlinearly with the number of
regions (cutout took 279 s and 46 GB on a 3 MP noisy photo). An image with an absurd number
of regions is still refused before tracing (posterize.check_regions, --max-regions).

Polygon mode keeps every path to absolute M/L/Z commands, which finalize can bake and merge.
Counters narrower than 2 pixels are lost: polygon simplification collapses a 1 px hole.
"""
import re
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

from . import fsio, posterize


def _vtrace(src, dst, *, hierarchical="stacked", speckle=4):
    import vtracer
    try:
        # Positional on purpose: vtracer 0.6.15's cp314 wheel is built with pyo3 0.19, and
        # keyword arguments to it segfault on Python 3.14. The order is
        # (image_path, out_path, colormode, hierarchical, mode, filter_speckle, ...).
        vtracer.convert_image_to_svg_py(str(src), str(dst), "color", hierarchical, "polygon",
                                        int(speckle))
    except BaseException as e:   # pyo3's PanicException derives from BaseException
        if isinstance(e, (KeyboardInterrupt, SystemExit, GeneratorExit)):
            raise
        raise ValueError(f"vtracer could not trace {src}: {e}") from None
    return re.findall(r'<path[^>]*?/>', Path(dst).read_text())


def trace_array(rgba, max_regions=None):
    """Trace a posterized RGBA array. Returns (svg text, number of colors). MAX_REGIONS as in
    posterize.check_regions (None: the default bound, 0: none)."""
    H, W = rgba.shape[:2]
    opaque = rgba[..., 3] >= 128
    colors = np.unique(rgba[opaque][:, :3], axis=0)
    if len(colors) == 0:
        raise ValueError("nothing to trace: the image is fully transparent")
    if len(colors) > posterize.MAX_COLORS:
        raise ValueError(f"the image has {len(colors)} colors; trace expects a posterized image "
                         "(run marquetry posterize first, or use marquetry coaster)")
    cls = np.full((H, W), len(colors), np.int32)
    for i, rgb in enumerate(colors):
        cls[opaque & np.all(rgba[..., :3] == rgb, axis=-1)] = i
    posterize.check_regions(cls, max_regions)   # fail fast on runaway input before tracing
    paths = []
    with tempfile.TemporaryDirectory(prefix="marquetry-") as tmp:
        src, dst = Path(tmp) / "layer.png", Path(tmp) / "layer.svg"
        for i, rgb in enumerate(colors):
            layer = np.zeros((H, W, 4), np.uint8)
            layer[cls == i] = (*rgb, 255)
            Image.fromarray(layer, "RGBA").save(src)
            paths += _vtrace(src, dst)
    if not paths:
        raise ValueError("nothing traceable: every region is narrower than 2 pixels")
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<svg version="1.1" xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}">\n'
            + "\n".join(paths) + "\n</svg>\n"), len(colors)


def trace(png, svg, max_regions=None):
    """Trace the posterized image PNG into the file SVG. Returns the number of colors."""
    text, n = trace_array(posterize.load_image(png), max_regions)   # vtracer panics on unreadable files
    svg = Path(svg).expanduser().absolute()
    with fsio.OutDir(svg.parent, explicit=True) as od:
        od.replace_all({svg.name: text}, protect=fsio.identities(png))
    return n


def add_arguments(ap):
    ap.add_argument("image", help="a posterized image: clean-color.png from marquetry posterize")
    ap.add_argument("svg", help="output SVG (e.g. raw.svg)")
    ap.add_argument("--max-regions", type=int, default=None, metavar="N",
                    help="refuse images with more than N separate regions (0 = no limit)")


def run(a):
    n = trace(a.image, a.svg, a.max_regions)
    print(f"traced {a.image} -> {a.svg} ({n} colors)")
