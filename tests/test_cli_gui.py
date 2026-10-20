import os
import shutil
import subprocess
import sys
import time

from PIL import Image

from conftest import PALETTE
from marquetry_svg import __version__
from marquetry_svg.cli import build_parser, main


def marquetry(*args, cwd=None):
    return subprocess.run([sys.executable, "-P", "-m", "marquetry_svg", *map(str, args)],
                          capture_output=True, text=True, cwd=cwd, timeout=120)


def test_help_lists_subcommands():
    r = marquetry("--help")
    assert r.returncode == 0
    for cmd in ("coaster", "posterize", "trace", "finalize", "gui"):
        assert cmd in r.stdout


def test_version():
    r = marquetry("--version")
    assert r.returncode == 0 and __version__ in r.stdout


def test_coaster_command(logo, tmp_path):
    out = tmp_path / "out"
    r = marquetry("coaster", logo, "--palette", PALETTE, "--base", "circle", "--preview",
                  "--out", out)
    assert r.returncode == 0, r.stderr
    assert "DONE" in r.stdout and "Only the top ~4 layers swap color" in r.stdout
    for name in ("test logo-design.svg", "test logo-coaster.svg", "test logo-disc.svg",
                 "test logo-emboss.svg", "preview-coaster.png", "preview-design.png"):
        assert (out / name).is_file(), name


def test_step_by_step_matches_one_shot(logo, tmp_path):
    work = tmp_path / "steps"
    assert marquetry("posterize", logo, work, "--palette", PALETTE).returncode == 0
    assert marquetry("trace", work / "clean-color.png", work / "raw.svg").returncode == 0
    r = marquetry("finalize", work / "raw.svg", work / "palette.json", "--out-prefix", work / "logo")
    assert r.returncode == 0, r.stderr
    one = tmp_path / "one"
    assert marquetry("coaster", logo, "--palette", PALETTE, "--out", one).returncode == 0
    for kind in ("design", "emboss", "disc", "coaster"):
        assert ((work / f"logo-{kind}.svg").read_text()
                == (one / f"test logo-{kind}.svg").read_text())


def test_bad_input_exits_2_with_a_message(logo, tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"")
    pj = tmp_path / "bad.json"
    pj.write_text('{"palette": [["a","b","c"]]}')
    raw = tmp_path / "raw.svg"
    raw.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>')
    cases = [
        (("coaster", tmp_path / "missing.png"), "no such image"),
        (("coaster", img, "--palette", "XYZ"), "hex"),
        (("coaster", img, "--palette", PALETTE), "cannot read"),
        (("coaster", logo, "--colors", "0"), "--colors"),
        (("coaster", logo, "--speckle", "99999999999999999999999"), "--speckle"),
        (("coaster", logo, "--base-color", "white"), "--base-color"),
        (("trace", img, tmp_path / "o.svg"), "cannot read"),
        (("trace", tmp_path / "missing.png", tmp_path / "o.svg"), "no such image"),
        (("finalize", raw, pj, "--out-prefix", tmp_path / "f"), "palette.json"),
        (("finalize", raw, tmp_path / "none.json", "--out-prefix", tmp_path / "f"), "no such"),
        (("finalize", raw, pj, "--out-prefix", tmp_path / "f", "--emboss-fill", "x"), "hex"),
    ]
    for args, msg in cases:
        r = marquetry(*args)
        assert r.returncode == 2, (args, r.stderr)
        assert msg in r.stderr, (args, r.stderr)
        assert "Traceback" not in r.stderr, (args, r.stderr)


def test_no_arguments_means_gui(monkeypatch):
    called = []
    import marquetry_svg.gui as gui
    monkeypatch.setattr(gui, "main", lambda: called.append(True) or 0)
    assert main([]) == 0 and called == [True]
    assert build_parser().parse_args(["gui"]).command == "gui"


# ---------------------------------------------------------------- GUI

_APP = []   # keep the QApplication alive for the whole session; Qt aborts on widgets without one


def qapp():
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    if not _APP:
        _APP.append(QApplication.instance() or QApplication([]))
    return _APP[0]


def test_gui_worker_runs_the_engine(logo):
    app = qapp()
    from marquetry_svg.gui import App, Worker
    window = App()
    window._add([str(logo), str(logo)])           # duplicates are ignored
    assert len(window._rows) == 1
    results = []
    w = Worker([str(logo)], 3, "circle")
    w.progress.connect(lambda *args: results.append(args))
    w.run()                                        # synchronously, in this thread
    assert results == [(str(logo), True, "3 parts", "")]
    design = (logo.parent / "test logo.png-coaster" / "test logo-design.svg").read_text()
    assert design.count("<path") == 3                 # the count is the design's paths
    window._on_progress(*results[0])
    assert window._rows[0].status() == "done"
    assert (logo.parent / "test logo.png-coaster" / "preview-coaster.png").is_file()
    window.close()
    app.processEvents()


def test_gui_reports_the_end_of_an_error(tmp_path):
    qapp()
    from marquetry_svg.gui import Worker, error_summary
    long_path = "/very/long/folder/" * 6 + "picture.png"
    msg = error_summary(f"Traceback...\nmarquetry: cannot read {long_path} as an image: bad\n")
    assert len(msg) == 60 and msg.startswith("cannot read") and msg.endswith("as an image: bad")
    assert error_summary("") == "error"
    bad = tmp_path / "bad.png"
    bad.write_bytes(b"nope")
    results = []
    w = Worker([str(bad)], 3, "circle")
    w.progress.connect(lambda *args: results.append(args))
    w.run()
    (path, ok, detail, full), = results
    assert not ok and detail.startswith("cannot read") and "as an image" in full


def test_gui_labels_are_plain_text(tmp_path):
    qapp()
    from PyQt6.QtCore import Qt
    from marquetry_svg.gui import FileRow
    img = tmp_path / "<b>bold<i>x.png"
    Image.new("RGB", (10, 10), "white").save(img)
    row = FileRow(str(img))
    assert row._name.textFormat() == Qt.TextFormat.PlainText
    assert row._name.text() == "<b>bold<i>x.png"
    assert row._detail.textFormat() == Qt.TextFormat.PlainText


def test_gui_reveals_only_a_real_folder(logo, tmp_path, monkeypatch):
    qapp()
    import marquetry_svg.gui as gui
    opened = []
    monkeypatch.setattr(gui.QDesktopServices, "openUrl", lambda url: opened.append(url))
    row = gui.FileRow(str(logo))
    target = tmp_path / "elsewhere"
    target.mkdir()
    (logo.parent / "test logo.png-coaster").symlink_to(target)
    row._reveal_out()
    assert opened == []
    (logo.parent / "test logo.png-coaster").unlink()
    (logo.parent / "test logo.png-coaster").mkdir()
    row._reveal_out()
    assert len(opened) == 1


def test_gui_stop_terminates_the_converter(tmp_path, monkeypatch):
    qapp()
    import marquetry_svg.gui as gui
    monkeypatch.setattr(gui, "engine_command",
                        lambda *a: [sys.executable, "-c", "import time; time.sleep(60)"])
    w = gui.Worker([str(tmp_path / "a.png"), str(tmp_path / "b.png")], 3, "circle")
    results = []
    w.progress.connect(lambda *args: results.append(args))
    w.start()
    deadline = time.time() + 10
    while w._proc is None and time.time() < deadline:
        time.sleep(0.02)
    proc = w._proc
    assert proc is not None
    t0 = time.time()
    w.stop()
    assert w.wait(5000)
    assert time.time() - t0 < 5
    assert proc.poll() is not None          # the converter is gone
    assert results == []                    # and the second image never started


def test_gui_window_icon_is_set():
    app = qapp()
    from marquetry_svg.gui import app_icon
    icon = app_icon()
    assert not icon.isNull()
    sizes = {(s.width(), s.height()) for s in icon.availableSizes()}
    assert {(32, 32), (512, 512)} <= sizes
    app.setWindowIcon(icon)
    assert not app.windowIcon().isNull()


def test_gui_engine_ignores_a_planted_package(logo, tmp_path):
    # A folder named marquetry_svg in the launch directory (or next to the image) must not
    # replace the installed package in the GUI's subprocess.
    from marquetry_svg.gui import engine_command

    trap = tmp_path / "trap"
    fake = trap / "marquetry_svg"
    fake.mkdir(parents=True)
    marker = tmp_path / "planted-code-ran"
    payload = f"open({str(marker)!r}, 'w').write('x')\n"
    (fake / "__init__.py").write_text(payload)
    (fake / "__main__.py").write_text(payload)
    image = trap / "logo.png"
    shutil.copy(logo, image)

    # Without -P the planted package wins, so this test can tell the difference.
    subprocess.run([sys.executable, "-m", "marquetry_svg", "--version"], cwd=trap,
                   capture_output=True, timeout=60)
    assert marker.exists()
    marker.unlink()

    cmd = engine_command(str(image), 3, "circle")
    assert cmd[1] == "-P"
    r = subprocess.run(cmd, cwd=trap, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert not marker.exists()
    assert (trap / "logo.png-coaster" / "logo-design.svg").is_file()
