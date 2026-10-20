"""Marquetry drag-and-drop app: image -> 3D-print coaster SVGs.

Drop a flat logo/sticker image; get filament-ready SVGs (one path per color +
base disc + emboss) next to it. Each image runs `marquetry coaster` in a
subprocess, so a bad image can't take the window down.
"""
import html
import os
import subprocess
import sys
import threading
from importlib.resources import files

from PyQt6.QtCore import Qt, QThread, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices, QIcon, QPixmap
from PyQt6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QSizePolicy, QFileDialog, QSpinBox,
)

from .coaster import out_dir_for
from .fsio import is_real_dir

IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
ACCENT = "#f6b820"   # marquetry amber

STYLE = f"""
QWidget {{ background: #1e1b16; color: #ece3d4; font-size: 13px; }}
QPushButton#go {{ background: {ACCENT}; color: #1e1b16; border: none;
    border-radius: 6px; padding: 8px 16px; font-weight: bold; }}
QPushButton#go:hover {{ background: #ffce4d; }}
QPushButton#go:disabled {{ background: #4a4338; color: #8a7f6c; }}
QPushButton#clear {{ background: transparent; color: #8a7f6c; border: none; }}
QPushButton#clear:hover {{ color: #ece3d4; }}
QPushButton.seg {{ background: #2c281f; color: #8a7f6c; border: 1px solid #3c372c;
    border-radius: 6px; padding: 5px 10px; }}
QPushButton.segOn {{ background: {ACCENT}; color: #1e1b16; border: 1px solid {ACCENT};
    border-radius: 6px; padding: 5px 10px; font-weight: bold; }}
QScrollArea {{ border: none; }}
QSpinBox {{ background: #2c281f; border: 1px solid #3c372c; border-radius: 6px; padding: 3px 6px; }}
QScrollBar:vertical {{ background: #1e1b16; width: 6px; }}
QScrollBar::handle:vertical {{ background: #4a4338; border-radius: 3px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
"""


TIMEOUT = 180   # seconds per image


def engine_command(path, colors, base):
    """The subprocess that converts one image. -P keeps the current directory off sys.path,
    so a folder named marquetry_svg wherever the app was launched (or next to the image) can't
    stand in for the installed package. (-I would also drop the user site, which breaks
    `pip install --user`.)"""
    return [sys.executable, "-P", "-m", "marquetry_svg", "coaster", path,
            "--colors", str(colors), "--base", base, "--preview"]


def error_summary(stderr, limit=60):
    """The useful end of a failed run's stderr: its last line without the program prefix,
    shortened in the middle when long."""
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    if not lines:
        return "error"
    msg = lines[-1]
    if msg.startswith("marquetry: "):
        msg = msg[len("marquetry: "):]
    if len(msg) > limit:
        head = (limit - 1) // 2
        msg = msg[:head] + "…" + msg[-(limit - 1 - head):]
    return msg


class Worker(QThread):
    progress = pyqtSignal(str, bool, str, str)   # path, ok, short detail, full message

    def __init__(self, paths, colors, base):
        super().__init__()
        self.paths, self.colors, self.base = paths, colors, base
        self._lock = threading.Lock()
        self._proc = None
        self._stopping = False

    def stop(self):
        """Stop after the current image and terminate its converter."""
        with self._lock:
            self._stopping = True
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()

    def run(self):
        for path in self.paths:
            try:
                with self._lock:
                    if self._stopping:
                        return
                    self._proc = subprocess.Popen(
                        engine_command(path, self.colors, self.base),
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                        cwd=os.path.dirname(os.path.abspath(path)),
                    )
                try:
                    out, err = self._proc.communicate(timeout=TIMEOUT)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.communicate()
                    self.progress.emit(path, False, "timeout", f"no result after {TIMEOUT} s")
                    continue
                if self._stopping:
                    return
                if self._proc.returncode != 0:
                    self.progress.emit(path, False, error_summary(err), err.strip()[-2000:])
                    continue
                parts = "?"   # paths in the design, one part per filament in the slicer
                for line in out.splitlines():
                    if "parts=" in line:
                        parts = line.split("parts=")[1].split()[0]
                        break
                self.progress.emit(path, True, f"{parts} parts", "")
            except Exception as e:
                self.progress.emit(path, False, error_summary(str(e)), str(e))
            finally:
                with self._lock:
                    self._proc = None


class FileRow(QFrame):
    removed = pyqtSignal(object)

    def __init__(self, path):
        super().__init__()
        self.path = path
        self.setStyleSheet("QFrame { background: #2c281f; border-radius: 8px; }")
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)

        self._thumb = QLabel()
        self._thumb.setFixedSize(44, 44)
        self._thumb.setStyleSheet("background: #1e1b16; border-radius: 6px;")
        self._thumb.setScaledContents(True)
        pm = QPixmap(path)
        if not pm.isNull():
            self._thumb.setPixmap(pm)
        row.addWidget(self._thumb)

        self._name = QLabel()
        self._name.setTextFormat(Qt.TextFormat.PlainText)   # file names are not markup
        self._name.setText(os.path.basename(path))
        self._name.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        row.addWidget(self._name)

        self._detail = QLabel("")
        self._detail.setTextFormat(Qt.TextFormat.PlainText)
        self._detail.setStyleSheet("color: #8a7f6c; font-size: 11px;")
        self._detail.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._detail)

        self._reveal = QPushButton("Reveal")
        self._reveal.setObjectName("clear")
        self._reveal.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reveal.clicked.connect(self._reveal_out)
        self._reveal.hide()
        row.addWidget(self._reveal)

        self._status = QLabel("queued")
        self._status.setStyleSheet("color: #8a7f6c; min-width: 64px;")
        self._status.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(self._status)

        rm = QPushButton("✕")
        rm.setObjectName("clear")
        rm.setCursor(Qt.CursorShape.PointingHandCursor)
        rm.clicked.connect(lambda: self.removed.emit(self))
        row.addWidget(rm)

    def _reveal_out(self):
        d = out_dir_for(self.path)
        if is_real_dir(d):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(d)))

    def status(self):
        return self._status.text()

    def set_running(self):
        self._status.setText("working…"); self._status.setStyleSheet(f"color: {ACCENT}; min-width: 64px;")

    def set_done(self, detail):
        self._status.setText("done"); self._status.setStyleSheet("color: #a9c98c; min-width: 64px;")
        self._detail.setText(detail)
        for name in ("preview-coaster.png", "preview-design.png"):
            png = out_dir_for(self.path) / name
            if png.exists():
                pm = QPixmap(str(png))
                if not pm.isNull():
                    self._thumb.setPixmap(pm)
                break
        self._reveal.show()

    def set_error(self, detail, full=""):
        self._status.setText("error"); self._status.setStyleSheet("color: #e08585; min-width: 64px;")
        self._detail.setText(detail)
        if full:
            self._detail.setToolTip(f"<p style='white-space:pre-wrap'>{html.escape(full)}</p>")


class DropZone(QLabel):
    files_dropped = pyqtSignal(list)

    def __init__(self):
        super().__init__("Drop logo images here  (PNG · JPG · WEBP)")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setAcceptDrops(True)
        self.setMinimumHeight(90)
        self._style(False)

    def _style(self, active):
        c = ACCENT if active else "#4a4338"
        t = ACCENT if active else "#8a7f6c"
        self.setStyleSheet(f"QLabel {{ border: 1.5px dashed {c}; border-radius: 10px; "
                           f"color: {t}; background: #2c281f; padding: 22px; font-size: 13px; }}")

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction(); self._style(True)

    def dragLeaveEvent(self, e):
        self._style(False)

    def dropEvent(self, e):
        self._style(False)
        paths = [u.toLocalFile() for u in e.mimeData().urls()
                 if os.path.splitext(u.toLocalFile())[1].lower() in IMAGES]
        if paths:
            self.files_dropped.emit(paths)


class App(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Marquetry")
        self.resize(580, 520)
        self.setStyleSheet(STYLE)
        self._rows = []
        self._worker = None
        self._base = "circle"
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 18, 22, 18)
        root.setSpacing(12)

        hdr = QHBoxLayout()
        title = QLabel("Marquetry")
        title.setStyleSheet(f"font-size: 22px; font-weight: bold; color: {ACCENT};")
        sub = QLabel("image → coaster")
        sub.setStyleSheet("color: #8a7f6c; font-size: 12px; padding-top: 6px;")
        hdr.addWidget(title); hdr.addWidget(sub); hdr.addStretch()
        root.addLayout(hdr)

        self._drop = DropZone()
        self._drop.files_dropped.connect(self._add)
        root.addWidget(self._drop)

        ctl = QHBoxLayout()
        ctl.addWidget(QLabel("Colors"))
        self._colors = QSpinBox(); self._colors.setRange(2, 8); self._colors.setValue(4)
        self._colors.setFixedWidth(56)
        ctl.addWidget(self._colors)
        ctl.addSpacing(16)
        ctl.addWidget(QLabel("Base"))
        self._base_btns = {}
        for key, lbl in (("circle", "● Circle"), ("square", "■ Square"), ("none", "None")):
            b = QPushButton(lbl)
            b.setProperty("class", "segOn" if key == self._base else "seg")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self._set_base(k))
            self._base_btns[key] = b
            ctl.addWidget(b)
        ctl.addStretch()
        browse = QPushButton("Browse…"); browse.setObjectName("clear")
        browse.setCursor(Qt.CursorShape.PointingHandCursor)
        browse.clicked.connect(self._browse)
        ctl.addWidget(browse)
        root.addLayout(ctl)

        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._list_w = QWidget(); self._list = QVBoxLayout(self._list_w)
        self._list.setContentsMargins(0, 0, 0, 0); self._list.setSpacing(6); self._list.addStretch()
        scroll.setWidget(self._list_w)
        root.addWidget(scroll)

        bar = QHBoxLayout()
        clear = QPushButton("Clear done"); clear.setObjectName("clear")
        clear.setCursor(Qt.CursorShape.PointingHandCursor)
        clear.clicked.connect(self._clear_done)
        bar.addWidget(clear); bar.addStretch()
        self._go = QPushButton("Make Coasters"); self._go.setObjectName("go")
        self._go.setCursor(Qt.CursorShape.PointingHandCursor)
        self._go.clicked.connect(self._run)
        bar.addWidget(self._go)
        root.addLayout(bar)

        self._hint = QLabel("Bambu: extrude the disc ~3 mm, emboss the design ~0.8 mm on top "
                            "→ only the top ~4 layers swap color.")
        self._hint.setStyleSheet("color: #6b6253; font-size: 10px;")
        self._hint.setWordWrap(True)
        root.addWidget(self._hint)

    def _set_base(self, key):
        self._base = key
        for k, b in self._base_btns.items():
            b.setProperty("class", "segOn" if k == key else "seg")
            b.setStyle(b.style())  # re-polish

    def _browse(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Select images", "",
                                                "Images (*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff *.gif)")
        self._add(paths)

    def _add(self, paths):
        existing = {r.path for r in self._rows}
        for p in paths:
            if p not in existing and os.path.isfile(p):
                existing.add(p)
                row = FileRow(p)
                row.removed.connect(self._remove)
                self._list.insertWidget(self._list.count() - 1, row)
                self._rows.append(row)

    def _remove(self, row):
        if row in self._rows:
            self._rows.remove(row)
        row.setParent(None)

    def _clear_done(self):
        for r in [r for r in list(self._rows) if r.status() in ("done", "error")]:
            self._remove(r)

    def _run(self):
        queued = [r for r in self._rows if r.status() == "queued"]
        if not queued or (self._worker and self._worker.isRunning()):
            return
        for r in queued:
            r.set_running()
        self._go.setEnabled(False)
        self._worker = Worker([r.path for r in queued], self._colors.value(), self._base)
        self._worker.progress.connect(self._on_progress)
        self._worker.finished.connect(lambda: self._go.setEnabled(True))
        self._worker.start()

    def _on_progress(self, path, ok, detail, full=""):
        for r in self._rows:
            if r.path == path:
                r.set_done(detail) if ok else r.set_error(detail, full)
                break

    def stop_worker(self):
        """Terminate a running conversion; called when the window closes or the app quits."""
        if self._worker is not None and self._worker.isRunning():
            self._worker.stop()
            self._worker.wait(5000)

    def closeEvent(self, event):
        self.stop_worker()
        super().closeEvent(event)


def app_icon():
    icon = QIcon()
    folder = files("marquetry_svg") / "icons"
    for name in ("icon-32.png", "icon-512.png"):
        icon.addFile(str(folder / name))
    return icon


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Marquetry")
    app.setWindowIcon(app_icon())
    window = App()
    app.aboutToQuit.connect(window.stop_worker)
    window.show()
    return app.exec()
