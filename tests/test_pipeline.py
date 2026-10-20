import json
import os
import re
import sys
import xml.etree.ElementTree as ET   # our own generated SVGs, not untrusted input

import numpy as np
import pytest
from PIL import Image, ImageDraw

import pixelcheck
from conftest import BLUE, BROWN, GREEN, PALETTE, RED, RING_CENTER, YELLOW, draw_logo, hexes
from marquetry_svg import coaster, finalize, posterize, preview, trace

SVG = "{http://www.w3.org/2000/svg}"


def render(svg):
    """Rasterize SVG at its own size; returns a pixel lookup (x, y) -> RGBA."""
    root = ET.parse(svg).getroot()
    W, H = int(root.get("width")), int(root.get("height"))
    import io
    im = Image.open(io.BytesIO(preview.render(svg.read_text(), W, H))).convert("RGBA")
    return im.getpixel


def run_coaster(image, **kw):
    if "palette" not in kw:
        kw["palette"] = posterize.parse_palette(PALETTE)
    return coaster.make_coaster(image, log=lambda *_: None, **kw)


def design_paths(out, stem):
    return ET.parse(out / f"{stem}-design.svg").getroot().findall(f"{SVG}path")


def assert_faithful(out, stem):
    """The design matches the posterized image within pixelcheck's bounds."""
    r = pixelcheck.check(out / f"{stem}-design.svg", out / "clean-color.png")
    assert pixelcheck.problems(r) == [], r
    return r


# ---------------------------------------------------------------- basic outputs

def test_outputs_land_next_to_the_image(logo):
    out, stem, _ = run_coaster(logo)
    assert out == logo.parent / "test logo.png-coaster"
    assert stem == "test logo"
    names = {p.name for p in out.iterdir()}
    assert names == set(coaster.output_names(stem)) - {"preview-coaster.png", "preview-design.png"}


def test_design_has_one_path_per_color_and_no_base(logo):
    out, stem, summary = run_coaster(logo)
    root = ET.parse(out / f"{stem}-design.svg").getroot()   # parses, so the SVG is well formed
    paths = root.findall(f"{SVG}path")
    assert len(paths) == 3 and summary["colors"] == 3
    assert {p.get("fill") for p in paths} == hexes(YELLOW, GREEN, BROWN)
    assert all(p.get("fill-rule") == "nonzero" for p in paths)
    assert root.find(f"{SVG}circle") is None and root.find(f"{SVG}rect") is None
    assert summary["paths"] == 4   # two green rings merged into one path
    assert_faithful(out, stem)


def test_translates_are_baked_into_absolute_coordinates(logo):
    out, stem, _ = run_coaster(logo)
    assert 'transform="translate' in (out / "raw.svg").read_text()   # vtracer's form
    for kind in ("design", "coaster", "emboss"):
        text = (out / f"{stem}-{kind}.svg").read_text()
        assert "transform" not in text
        assert not re.search(r'd="[^"]*[a-y]', text)   # absolute commands only


def test_letter_counters_stay_holes(logo):
    out, stem, _ = run_coaster(logo)
    px = render(out / f"{stem}-design.svg")
    cx, cy = RING_CENTER
    assert px((cx, cy))[3] == 0                  # the O's counter is transparent
    assert px((cx + 45, cy))[:3] == GREEN        # its band is green
    assert px((300, 280))[3] == 0                # the 6 px counter of the small ring survives
    assert px((292, 280))[:3] == GREEN
    assert px((10, 10))[3] == 0                  # background transparent
    assert px((200, 60))[:3] == YELLOW
    assert px((290, 190))[:3] == BROWN


def test_coaster_and_disc_carry_the_base(logo):
    out, stem, _ = run_coaster(logo, base="circle", base_color="123456")
    disc = ET.parse(out / f"{stem}-disc.svg").getroot()
    assert disc.find(f"{SVG}circle").get("fill") == "#123456"   # normalized hex
    assert disc.findall(f"{SVG}path") == []
    coaster_svg = ET.parse(out / f"{stem}-coaster.svg").getroot()
    assert coaster_svg.find(f"{SVG}circle") is not None
    assert len(coaster_svg.findall(f"{SVG}path")) == 3
    px = render(out / f"{stem}-coaster.svg")
    assert px(RING_CENTER)[:3] == (0x12, 0x34, 0x56)   # base shows through the O


def test_square_and_no_base(logo, tmp_path):
    out, stem, _ = run_coaster(logo, base="square")
    assert ET.parse(out / f"{stem}-disc.svg").getroot().find(f"{SVG}rect") is not None
    out2, stem2, _ = run_coaster(logo, base="none", out=tmp_path / "nobase")
    names = {p.name for p in out2.iterdir()}
    assert f"{stem2}-design.svg" in names and f"{stem2}-emboss.svg" in names
    assert f"{stem2}-disc.svg" not in names and f"{stem2}-coaster.svg" not in names


def test_emboss_is_one_part(logo):
    out, stem, _ = run_coaster(logo)
    paths = ET.parse(out / f"{stem}-emboss.svg").getroot().findall(f"{SVG}path")
    assert len(paths) == 1 and paths[0].get("fill") == "#1A1A1A"
    px = render(out / f"{stem}-emboss.svg")
    assert px(RING_CENTER)[3] == 0 and px((200, 60))[3] == 255 and px((290, 190))[3] == 255


def test_previews(logo, tmp_path):
    out, stem, _ = run_coaster(logo, make_preview=True)
    for kind in ("coaster", "design"):
        assert Image.open(out / f"preview-{kind}.png").size == (500, 500)
    msgs = []
    out2, _, _ = coaster.make_coaster(logo, palette=posterize.parse_palette(PALETTE), base="none",
                                      out=tmp_path / "nb", make_preview=True, log=msgs.append)
    assert msgs[-1] == "previews: preview-design.png"     # listed once, no coaster preview
    assert not (out2 / "preview-coaster.png").exists()


def test_auto_kmeans_finds_the_three_inks(logo):
    out, stem, summary = run_coaster(logo, palette=None, colors=3)
    assert summary["colors"] == 3
    got = json.loads((out / "palette.json").read_text())["palette"]
    for want in (YELLOW, GREEN, BROWN):
        assert min(sum((a - b) ** 2 for a, b in zip(c, want)) for c in got) < 30
    assert_faithful(out, stem)


def test_palette_longer_than_colors_keeps_every_ink(tmp_path):
    # Regression: the background index used to be --colors (default 4), so the fifth
    # color of an explicit 5-color palette was treated as background and dropped.
    inks = [RED, (30, 160, 40), BLUE, (230, 180, 20), (120, 40, 140)]
    im = Image.new("RGB", (500, 200), "white")
    d = ImageDraw.Draw(im)
    for i, c in enumerate(inks):
        d.rectangle((20 + i * 95, 50, 95 + i * 95, 150), fill=c)
    img = tmp_path / "five.png"
    im.save(img)
    pal = ",".join("%02X%02X%02X" % c for c in inks)
    out, stem, summary = run_coaster(img, palette=posterize.parse_palette(pal))
    assert summary["colors"] == 5
    assert {p.get("fill") for p in design_paths(out, stem)} == hexes(*inks)


# ------------------------------------------------- images that used to go wrong silently

def test_logo_jpeg_on_near_white_drops_the_background(tmp_path):
    # the original use case: a flat logo JPEG on an off-white background
    img = draw_logo(tmp_path / "logo.jpg", bg=(248, 247, 243))
    for palette in (posterize.parse_palette(PALETTE), None):
        out, stem, summary = run_coaster(img, palette=palette, colors=3,
                                         out=tmp_path / ("explicit" if palette else "auto"))
        assert summary["colors"] == 3
        px = render(out / f"{stem}-design.svg")
        assert px((5, 5))[3] == 0 and px((395, 315))[3] == 0 and px(RING_CENTER)[3] == 0
        assert_faithful(out, stem)


def test_transparent_png_background_is_not_ink(tmp_path):
    # transparent pixels stored as black used to become a black part covering everything
    img = draw_logo(tmp_path / "alpha.png", bg=(0, 0, 0, 0), mode="RGBA")
    out, stem, summary = run_coaster(img, palette=None, colors=3)
    assert summary["colors"] == 3
    fills = {p.get("fill") for p in design_paths(out, stem)}
    assert "#000000" not in fills and len(fills) == 3
    px = render(out / f"{stem}-design.svg")
    assert px((5, 5))[3] == 0 and px(RING_CENTER)[3] == 0
    assert_faithful(out, stem)


def test_tight_crop_keeps_the_edge_touching_color(tmp_path):
    # a badge cropped to its edge: the green disc covers 90%+ of the canvas and must survive
    im = Image.new("RGB", (300, 300), "white")
    d = ImageDraw.Draw(im)
    d.ellipse((0, 0, 299, 299), fill=GREEN)
    d.rectangle((90, 130, 210, 170), fill=YELLOW)
    img = tmp_path / "badge.png"
    im.save(img)
    out, stem, _ = run_coaster(img, palette=posterize.parse_palette("0D5E1E,F6B820"))
    assert {p.get("fill") for p in design_paths(out, stem)} == hexes(GREEN, YELLOW)
    px = render(out / f"{stem}-design.svg")
    assert px((150, 40))[:3] == GREEN and px((150, 150))[:3] == YELLOW
    assert_faithful(out, stem)


def test_color_on_color_parts_do_not_overlap(tmp_path):
    im = Image.new("RGB", (300, 300), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((30, 30, 270, 270), fill=YELLOW)
    d.ellipse((90, 90, 210, 210), fill=GREEN)
    d.ellipse((130, 130, 170, 170), fill="white")   # a real hole through both
    img = tmp_path / "onto.png"
    im.save(img)
    out, stem, _ = run_coaster(img, palette=posterize.parse_palette("F6B820,0D5E1E"))
    r = assert_faithful(out, stem)
    assert r["overlap_width"] <= 1.5
    masks, _ = pixelcheck.path_masks((out / f"{stem}-design.svg").read_text(), k=1)
    yellow, green = masks["#F6B820"], masks["#0D5E1E"]
    assert not yellow[150, 110] and green[150, 110]      # yellow is cut out under the green
    assert not yellow[150, 150] and not green[150, 150]  # and the hole goes through both


def test_small_icon_keeps_small_edge_details(tmp_path):
    # 48 px: a 6x6 corner mark touching the edge used to fall under the absolute 800 px^2
    # edge-speck limit
    im = Image.new("RGB", (48, 48), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((8, 8, 39, 39), fill=BLUE)
    d.rectangle((18, 18, 29, 29), fill=YELLOW)
    d.rectangle((0, 0, 5, 5), fill=RED)
    img = tmp_path / "icon48.png"
    im.save(img)
    out, stem, summary = run_coaster(img, palette=posterize.parse_palette("1E3CC8,F6B820,C81E1E"))
    assert summary["dropped"]["speck"] == 0
    assert {p.get("fill") for p in design_paths(out, stem)} == hexes(BLUE, YELLOW, RED)
    assert_faithful(out, stem)


def test_exif_rotated_jpeg_is_used_upright(tmp_path):
    upright = Image.new("RGB", (200, 400), "white")
    d = ImageDraw.Draw(upright)
    d.rectangle((20, 20, 180, 180), fill=RED)       # red on top
    d.rectangle((20, 220, 180, 380), fill=BLUE)     # blue below
    raw = upright.transpose(Image.Transpose.ROTATE_90)   # stored sideways...
    exif = Image.Exif()
    exif[0x0112] = 6                                     # ...with "rotate 90 CW to display"
    img = tmp_path / "phone.jpg"
    raw.save(img, exif=exif, quality=95)
    out, stem, _ = run_coaster(img, palette=posterize.parse_palette("C81E1E,1E3CC8"))
    assert json.loads((out / "palette.json").read_text())["size"] == [200, 400]
    px = render(out / f"{stem}-design.svg")
    assert px((100, 100))[:3] == RED and px((100, 300))[:3] == BLUE
    assert_faithful(out, stem)


def test_sixteen_bit_greyscale(tmp_path):
    a = np.full((60, 60), 65535, np.uint16)
    a[10:50, 10:50] = 8000
    img = tmp_path / "g16.png"
    Image.fromarray(a).save(img)
    out, stem, summary = run_coaster(img, palette=None, colors=1)
    assert summary["colors"] == 1
    assert render(out / f"{stem}-design.svg")((30, 30))[3] == 255


# ---------------------------------------------------------------- specks and counters

def tiny_counters(path):
    """Green squares with a 2x2 and a 1x1 counter."""
    im = Image.new("RGB", (80, 40), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((5, 5, 30, 30), fill=GREEN)
    d.rectangle((17, 17, 18, 18), fill="white")
    d.rectangle((45, 5, 70, 30), fill=GREEN)
    d.point((57, 17), fill="white")
    im.save(path)
    return path


def test_two_pixel_counters_survive_and_median_would_close_them(tmp_path):
    img = tiny_counters(tmp_path / "counters.png")
    out, stem, _ = run_coaster(img, palette=posterize.parse_palette("0D5E1E"))
    px = render(out / f"{stem}-design.svg")
    assert px((17, 17))[3] < 128 and px((18, 18))[3] < 128   # the 2x2 counter stays open
    assert px((10, 10))[3] == 255
    # the 1x1 counter survives posterize (below) but not vtracer's polygon simplification,
    # which collapses a one-pixel hole; the README lists this limit
    assert px((57, 17))[3] == 255
    # control: the median despeckle (off by default) would have closed both
    rgba = posterize.load_image(img)
    pal = posterize.parse_palette("0D5E1E")
    _, kept = posterize.quantize(rgba, palette=pal)
    _, closed = posterize.quantize(rgba, palette=pal, median=3)
    assert kept[17, 17] == 1 and kept[17, 57] == 1      # background class
    assert closed[17, 17] == 0 and closed[17, 57] == 0  # filled with green


def test_speckle_drops_ink_specks_on_transparent_input(tmp_path):
    im = Image.new("RGBA", (100, 60), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rectangle((10, 10, 50, 50), fill=GREEN + (255,))
    d.rectangle((70, 20, 71, 21), fill=RED + (255,))     # 2x2 red speck on background
    d.rectangle((30, 30, 31, 31), fill=RED + (255,))     # 2x2 red speck inside the green
    img = tmp_path / "specks.png"
    im.save(img)
    pal = posterize.parse_palette("0D5E1E,C81E1E")
    rgba = posterize.load_image(img)
    _, cls = posterize.quantize(rgba, palette=pal)          # default --speckle 4
    assert cls[20, 70] == 2                                 # became background
    assert cls[30, 30] == 0                                 # became green, not a hole
    _, keep = posterize.quantize(rgba, palette=pal, speckle=1)
    assert keep[20, 70] == 1 and keep[30, 30] == 1
    out, stem, _ = run_coaster(img, palette=pal, speckle=1)
    assert len(design_paths(out, stem)) == 2
    out, stem, _ = run_coaster(img, palette=pal, out=tmp_path / "default")
    assert len(design_paths(out, stem)) == 1


def test_speckle_that_removes_everything_is_an_error(tmp_path):
    im = Image.new("RGB", (10, 10), "white")
    ImageDraw.Draw(im).rectangle((3, 3, 5, 5), fill=RED)   # 3x3, under the default 4x4
    img = tmp_path / "dot.png"
    im.save(img)
    with pytest.raises(ValueError, match="speck"):
        run_coaster(img, palette=posterize.parse_palette("C81E1E"))
    assert not (tmp_path / "dot.png-coaster").exists()
    out, stem, _ = run_coaster(img, palette=posterize.parse_palette("C81E1E"), speckle=1)
    assert len(design_paths(out, stem)) == 1


# ---------------------------------------------------------------- tracing

def test_small_details_inside_another_color_survive(tmp_path):
    # vtracer's multi-color cutout mode merges anything under ~10x10 px into the color around
    # it; tracing one color at a time keeps them
    im = Image.new("RGB", (300, 120), "white")
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((5, 5, 295, 115), 20, fill=YELLOW)
    spots = []
    for i, size in enumerate((3, 4, 6, 8, 10)):
        x = 30 + i * 50
        d.rectangle((x, 30, x + size - 1, 30 + size - 1), fill=RED)
        d.ellipse((x, 70, x + size + 1, 70 + size + 1), fill=GREEN)
        spots.append((x + size // 2, 30 + size // 2, x + (size + 1) // 2, 70 + (size + 1) // 2))
    img = tmp_path / "eyes.png"
    im.save(img)
    out, stem, _ = run_coaster(img, palette=posterize.parse_palette("F6B820,C81E1E,0D5E1E"),
                               speckle=1)
    px = render(out / f"{stem}-design.svg")
    for rx, ry, gx, gy in spots:
        assert px((rx, ry))[:3] == RED, (rx, ry)
        assert px((gx, gy))[:3] == GREEN, (gx, gy)
    assert_faithful(out, stem)


def test_a_single_pixel_image_is_an_error_not_an_empty_svg(tmp_path):
    Image.new("RGB", (1, 1), RED).save(tmp_path / "one.png")
    with pytest.raises(ValueError, match="narrower than 2 pixels"):
        run_coaster(tmp_path / "one.png", palette=None, colors=1, speckle=1)
    assert not (tmp_path / "one.png-coaster" / "one-emboss.svg").exists()
    assert not (tmp_path / "one.png-coaster" / "one-design.svg").exists()


def test_trace_refuses_an_unposterized_image(tmp_path):
    a = np.zeros((40, 40, 3), np.uint8)
    a[..., 0] = np.arange(40)[:, None] * 6
    a[..., 1] = np.arange(40)[None, :] * 6
    Image.fromarray(a).save(tmp_path / "gradient.png")
    with pytest.raises(ValueError, match="posterized"):
        trace.trace(tmp_path / "gradient.png", tmp_path / "g.svg")
    Image.new("RGBA", (10, 10), (0, 0, 0, 0)).save(tmp_path / "clear.png")
    with pytest.raises(ValueError, match="transparent"):
        trace.trace(tmp_path / "clear.png", tmp_path / "c.svg")


def dot_on_white(path, size):
    im = Image.new("RGB", (120, 80), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((10, 10, 10 + size - 1, 10 + size - 1), fill=RED)
    d.rectangle((40, 10, 110, 70), fill=YELLOW)
    im.save(path)
    return path


def has_red(paths):
    return any('fill="#C81E1E"' in p for p in paths)


def test_vtracer_argument_slots(tmp_path):
    # _vtrace passes vtracer's arguments positionally; fixtures where each slot changes the
    # result pin what the slots mean
    small, mid = dot_on_white(tmp_path / "d3.png", 3), dot_on_white(tmp_path / "d8.png", 8)
    out = tmp_path / "o.svg"
    # slot 6, filter_speckle: a 3x3 dot survives speckle 2 and goes at 4
    assert has_red(trace._vtrace(small, out, hierarchical="stacked", speckle=2))
    assert not has_red(trace._vtrace(small, out, hierarchical="stacked", speckle=4))
    # slot 4, hierarchical: stacked keeps an 8x8 dot, multi-color cutout merges it away
    assert has_red(trace._vtrace(mid, out, hierarchical="stacked", speckle=1))
    assert not has_red(trace._vtrace(mid, out, hierarchical="cutout", speckle=1))


@pytest.mark.skipif(sys.version_info >= (3, 14), reason="keyword calls into vtracer 0.6.15 crash on 3.14")
def test_positional_vtracer_call_matches_keywords(tmp_path):
    import vtracer
    img = dot_on_white(tmp_path / "d.png", 3)
    for hier in ("stacked", "cutout"):
        for speck in (1, 2, 4):
            pos = trace._vtrace(img, tmp_path / "pos.svg", hierarchical=hier, speckle=speck)
            vtracer.convert_image_to_svg_py(str(img), str(tmp_path / "kw.svg"), colormode="color",
                                            hierarchical=hier, mode="polygon",
                                            filter_speckle=speck)
            assert (tmp_path / "pos.svg").read_text() == (tmp_path / "kw.svg").read_text()


def test_trace_turns_vtracer_panics_into_errors(tmp_path, monkeypatch):
    import vtracer
    PanicException = type("PanicException", (BaseException,), {})

    def boom(*a, **k):
        raise PanicException("called `Result::unwrap()` on an `Err` value")
    monkeypatch.setattr(vtracer, "convert_image_to_svg_py", boom)
    img = dot_on_white(tmp_path / "x.png", 8)
    with pytest.raises(ValueError, match="vtracer could not trace"):
        trace.trace(img, tmp_path / "x.svg")
    monkeypatch.undo()
    (tmp_path / "notimage.png").write_text("hello")
    with pytest.raises(ValueError, match="cannot read"):
        trace.trace(tmp_path / "notimage.png", tmp_path / "y.svg")


# ---------------------------------------------------------------- output folders and files

def test_output_folders_do_not_collide(tmp_path):
    names = ("logo.png", "logo.jpg", "my logo.png", "my_logo.png", ".logo.png")
    dirs = {coaster.out_dir_for(draw_logo(tmp_path / n)) for n in names}
    assert len(dirs) == len(names)
    assert coaster.out_dir_for(tmp_path / ".logo.png") == tmp_path / ".logo.png-coaster"
    assert coaster.file_stem(tmp_path / ".logo.png") == "logo"     # outputs not hidden
    assert coaster.out_dir_for(tmp_path / "..png") == tmp_path / "..png-coaster"
    assert coaster.file_stem(tmp_path / "..png") == "png"
    assert coaster.file_stem(tmp_path / "...") == "image"
    weird = tmp_path / "..png"
    Image.open(draw_logo(tmp_path / "w.png")).save(weird, format="PNG")
    out, stem, _ = run_coaster(weird)
    assert out.name == "..png-coaster" and (out / "png-design.svg").is_file()


def test_rerun_clears_stale_outputs(logo):
    out, stem, _ = run_coaster(logo, make_preview=True)
    assert (out / f"{stem}-disc.svg").exists() and (out / "preview-coaster.png").exists()
    (out / "notes.txt").write_text("mine")
    out, stem, _ = run_coaster(logo, base="none")
    names = {p.name for p in out.iterdir()}
    assert f"{stem}-disc.svg" not in names and f"{stem}-coaster.svg" not in names
    assert "preview-coaster.png" not in names and "preview-design.png" not in names
    assert "notes.txt" in names     # only Marquetry's own outputs are cleared
    assert not [n for n in names if n.endswith(".tmp")]


def snapshot(folder):
    return {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file() and not p.is_symlink()}


def test_a_refusal_leaves_earlier_outputs_untouched(logo, tmp_path):
    out, stem, _ = run_coaster(logo, make_preview=True)
    before = snapshot(out)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (out / "raw.svg").unlink()
    (out / "raw.svg").symlink_to(victim)          # one bad name among the outputs
    with pytest.raises(ValueError, match="symlink"):
        run_coaster(logo, base="none", palette=posterize.parse_palette("F6B820,0D5E1E"))
    after = snapshot(out)
    del before["raw.svg"]
    assert after == before                        # nothing was deleted or replaced
    assert victim.read_text() == "keep me"


def test_a_failed_write_leaves_earlier_outputs_and_no_temp_files(logo, monkeypatch):
    from marquetry_svg import fsio
    out, stem, _ = run_coaster(logo)
    before = snapshot(out)
    real_fdopen, calls = os.fdopen, []

    def flaky(fd, *a, **k):
        calls.append(fd)
        if len(calls) == 3:
            os.close(fd)
            raise OSError(28, "No space left on device")
        return real_fdopen(fd, *a, **k)
    monkeypatch.setattr(fsio.os, "fdopen", flaky)
    with pytest.raises(OSError):
        run_coaster(logo, base="none")
    monkeypatch.undo()
    assert snapshot(out) == before


def test_symlinked_default_folder_is_refused(logo, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (logo.parent / "test logo.png-coaster").symlink_to(target)
    with pytest.raises(ValueError, match="symlink"):
        run_coaster(logo)
    assert list(target.iterdir()) == []


def test_symlinked_folder_given_as_out_is_used(logo, tmp_path):
    target = tmp_path / "real-results"
    target.mkdir()
    (tmp_path / "results").symlink_to(target)
    out, stem, _ = run_coaster(logo, out=tmp_path / "results")
    assert (target / f"{stem}-design.svg").is_file()
    # the files inside are still never followed
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (target / f"{stem}-emboss.svg").unlink()
    (target / f"{stem}-emboss.svg").symlink_to(victim)
    with pytest.raises(ValueError, match="symlink"):
        run_coaster(logo, out=tmp_path / "results")
    assert victim.read_text() == "keep me"


def test_symlinked_output_file_is_refused(logo, tmp_path):
    out = coaster.out_dir_for(logo)
    out.mkdir()
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    (out / "test logo-design.svg").symlink_to(victim)
    with pytest.raises(ValueError, match="symlink"):
        run_coaster(logo)
    assert victim.read_text() == "keep me"
    assert (out / "test logo-design.svg").is_symlink()


def test_out_path_is_absolute_not_resolved(logo, tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "via").symlink_to(real)
    out, _, _ = run_coaster(logo, out=tmp_path / "via" / "result")
    assert out == tmp_path / "via" / "result"           # not .../real/result
    monkeypatch.chdir(tmp_path)
    out, _, _ = run_coaster(logo, out="rel")
    assert out == tmp_path / "rel" and out.is_absolute()
    assert (real / "result" / "test logo-design.svg").is_file()


def test_input_with_an_output_name_is_not_overwritten(tmp_path):
    img = draw_logo(tmp_path / "clean-color.png")
    with pytest.raises(ValueError, match="one of the inputs"):
        run_coaster(img, out=tmp_path)
    assert Image.open(img).size == (400, 320)


def test_input_name_check_ignores_case(tmp_path):
    # APFS is case-insensitive: Clean-Color.PNG is the file an output called clean-color.png
    # would replace, so the check compares files, not spellings
    img = draw_logo(tmp_path / "Clean-Color.PNG")
    probe = tmp_path / "clean-color.png"
    if not probe.exists():
        pytest.skip("case-sensitive file system")
    with pytest.raises(ValueError, match="one of the inputs"):
        run_coaster(img, out=tmp_path)
    assert Image.open(img).size == (400, 320)


def test_bad_input_creates_no_folder(tmp_path):
    (tmp_path / "broken.png").write_bytes(b"\x89PNG\r\n\x1a\n not really")
    with pytest.raises(ValueError, match="cannot read"):
        run_coaster(tmp_path / "broken.png")
    Image.new("RGB", (50, 50), "white").save(tmp_path / "blank.png")
    with pytest.raises(ValueError, match="background"):
        run_coaster(tmp_path / "blank.png")
    with pytest.raises(FileNotFoundError):
        run_coaster(tmp_path / "nope.png")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["blank.png", "broken.png"]


# ---------------------------------------------------------------- validation and helpers

def test_settings_are_validated(logo):
    with pytest.raises(ValueError, match="--colors"):
        run_coaster(logo, palette=None, colors=0)
    with pytest.raises(ValueError, match="--speckle"):
        run_coaster(logo, speckle=10 ** 30)
    with pytest.raises(ValueError, match="--base-color"):
        run_coaster(logo, base_color="red")
    with pytest.raises(ValueError, match="--base-color"):
        run_coaster(logo, base_color='#123456"/><script')
    assert not coaster.out_dir_for(logo).exists()


def test_parse_palette_and_hex():
    assert posterize.parse_palette("#F6B820, 0d5e1e,AD6D1A") == [YELLOW, GREEN, BROWN]
    for bad in ("F6B8,0D5E1E", " , ", "ZZZZZZ"):
        with pytest.raises(ValueError):
            posterize.parse_palette(bad)
    assert posterize.parse_hex("abcdef") == "#ABCDEF"


def test_decompression_bomb_is_a_clean_error(logo, monkeypatch):
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(ValueError, match="too large"):
        posterize.load_image(logo)


def test_nearest_in_chunks_matches_one_block(monkeypatch):
    rng = np.random.default_rng(1)
    flat = rng.integers(0, 256, (5000, 3)).astype(np.float32)
    pal = [YELLOW, GREEN, BROWN, RED]
    whole = posterize.nearest(flat, pal)
    monkeypatch.setattr(posterize, "CHUNK", 777)
    assert (posterize.nearest(flat, pal) == whole).all()


def test_bake():
    assert finalize._bake("M0,0 L10,5 Z", 100, 200) == "M100.00,200.00 L110.00,205.00 Z"
    assert finalize._bake("m0,0 l10,5 z", 1, 1) is None   # relative commands are not baked
    assert finalize._bake("M0,0 H10 Z", 1, 1) is None      # H/V/A break x/y parity


OPAQUE_TRACE = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">\n'
    '<path d="M0,0 L200,0 L200,100 L0,100 Z " fill="#FEFEFE" transform="translate(0,0)"/>\n'
    '<path d="M0,0 L40,0 L40,40 L0,40 Z " fill="#F5B921" transform="translate(20,20)"/>\n'
    '<path d="M0,0 L30,0 L30,30 L0,30 Z " fill="#F6B820" transform="translate(120,30)"/>\n'
    '<path d="M0,0 L5,0 L5,5 L0,5 Z " fill="#0D5E1E" transform="translate(0,95)"/>\n'
    '<path d="m0,0 l10,0 l0,10 z" fill="#0D5E1E" transform="translate(80,40)"/>\n'
    '</svg>\n')


def test_finalize_drop_bg_is_opt_in(tmp_path):
    raw = tmp_path / "raw.svg"
    raw.write_text(OPAQUE_TRACE)
    pj = tmp_path / "palette.json"
    pj.write_text(json.dumps({"palette": [list(YELLOW), list(GREEN)], "size": [200, 100]}))
    r = finalize.finalize(raw, pj, tmp_path / "x", base="none", drop_bg=True, speck_area=800)
    assert r["bg"] == "#FEFEFE" and r["dropped"] == {"bg": 1, "speck": 1, "empty": 0}
    assert r["colors"] == 1 and r["paths"] == 2 and r["unmerged"] == 1
    paths = ET.parse(tmp_path / "x-design.svg").getroot().findall(f"{SVG}path")
    merged = [p for p in paths if p.get("transform") is None]
    assert len(merged) == 1 and merged[0].get("fill") == "#F6B820"   # snapped to the palette
    assert merged[0].get("d").count("M") == 2
    assert [p.get("fill") for p in paths if p.get("transform")] == ["#0D5E1E"]   # left un-merged
    r = finalize.finalize(raw, pj, tmp_path / "y", base="none")
    assert r["bg"] is None and r["dropped"]["bg"] == 0     # default keeps a full-canvas path
    assert r["dropped"]["speck"] == 0                      # and small edge paths


@pytest.mark.parametrize("content, match", [
    ('{"pal": []}', "palette.json"),
    ('{"palette": [["a","b","c"]], "size": [400, 320]}', "palette.json"),
    ('{"palette": [[1, 2]]}', "R, G, B"),
    ('{"palette": [[300, 0, 0]]}', "R, G, B"),
    ('{"palette": [[1, 2, 3]], "size": [0, 5]}', "positive"),
    ('[1, 2, 3]', "palette.json"),
    ('not json', "palette.json"),
])
def test_bad_palette_json(tmp_path, content, match):
    (tmp_path / "raw.svg").write_text(OPAQUE_TRACE)
    (tmp_path / "p.json").write_text(content)
    with pytest.raises(ValueError, match=match):
        finalize.finalize(tmp_path / "raw.svg", tmp_path / "p.json", tmp_path / "x")


def test_finalize_validates_colors(tmp_path):
    (tmp_path / "raw.svg").write_text(OPAQUE_TRACE)
    (tmp_path / "p.json").write_text('{"palette": [[1, 2, 3]]}')
    for kw in ({"base_color": "white"}, {"emboss_fill": "#12"}):
        with pytest.raises(ValueError, match="hex"):
            finalize.finalize(tmp_path / "raw.svg", tmp_path / "p.json", tmp_path / "x", **kw)


# ---------------------------------------------------------------- the output folder is opened once

def test_outdir_writes_stay_in_the_opened_folder_after_a_swap(tmp_path):
    from marquetry_svg import fsio
    out, moved, victim = tmp_path / "out", tmp_path / "moved", tmp_path / "victim"
    victim.mkdir()
    with fsio.OutDir(out) as od:
        od.replace_all({"a.svg": "first"})
        out.rename(moved)               # swap the folder for a symlink mid-run
        out.symlink_to(victim)
        od.replace_all({"a.svg": "second", "b.png": b"x"})
        od.replace_all({"c.svg": "third"}, stale=["a.svg"])
    assert list(victim.iterdir()) == []
    assert sorted(p.name for p in moved.iterdir()) == ["b.png", "c.svg"]


def test_coaster_run_survives_a_swapped_folder(logo, tmp_path, monkeypatch):
    from marquetry_svg import fsio
    out = coaster.out_dir_for(logo)
    moved, victim = tmp_path / "moved", tmp_path / "victim"
    victim.mkdir()
    real_replace = fsio.OutDir.replace_all

    def swap_then_write(self, *a, **k):
        out.rename(moved)
        out.symlink_to(victim)
        return real_replace(self, *a, **k)
    monkeypatch.setattr(fsio.OutDir, "replace_all", swap_then_write)
    run_coaster(logo, make_preview=True)
    assert list(victim.iterdir()) == []
    assert {"raw.svg", "test logo-design.svg", "preview-design.png"} <= {p.name for p in moved.iterdir()}


def test_outdir_refuses_what_it_should(tmp_path, monkeypatch):
    from marquetry_svg import fsio
    (tmp_path / "file").write_text("x")
    with pytest.raises(ValueError, match="not a folder"):
        fsio.OutDir(tmp_path / "file")
    (tmp_path / "link").symlink_to(tmp_path)
    with pytest.raises(ValueError, match="symlink"):
        fsio.OutDir(tmp_path / "link")
    fsio.OutDir(tmp_path / "link", explicit=True).close()   # a folder the user named
    with fsio.OutDir(tmp_path / "ok") as od:
        for bad in ("../escape.svg", "a/b.svg", "..", ""):
            with pytest.raises(ValueError, match="plain file name"):
                od.replace_all({bad: "x"})
    me = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: me + 1)
    with pytest.raises(ValueError, match="you own"):
        fsio.OutDir(tmp_path / "ok")


# ---------------------------------------------------------------- pixelcheck itself

def mutate(svg_path, fn):
    text = svg_path.read_text()
    out = svg_path.with_name("mutant-" + svg_path.name)
    out.write_text(fn(text))
    return out


def test_pixelcheck_catches_a_one_pixel_dilation(logo):
    out, stem, _ = run_coaster(logo)
    design = out / f"{stem}-design.svg"
    assert pixelcheck.problems(pixelcheck.check(design, out / "clean-color.png")) == []
    fat = mutate(design, lambda t: re.sub(r'<path fill="(#[0-9A-F]{6})"',
                                          r'<path stroke="\1" stroke-width="2" fill="\1"', t))
    r = pixelcheck.check(fat, out / "clean-color.png")
    assert r["max_area_drift"] > 0.8 and r["leak_width"] > 1.6
    assert pixelcheck.problems(r)


def test_pixelcheck_catches_a_vanished_small_dot(tmp_path):
    im = Image.new("RGB", (120, 60), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((10, 10, 100, 50), fill=YELLOW)
    d.rectangle((30, 28, 33, 31), fill=RED)           # a 4x4 red dot
    img = tmp_path / "dot.png"
    im.save(img)
    out, stem, _ = run_coaster(img, palette=posterize.parse_palette("F6B820,C81E1E"), speckle=1)
    design = out / f"{stem}-design.svg"
    assert pixelcheck.problems(pixelcheck.check(design, out / "clean-color.png")) == []
    gone = mutate(design, lambda t: re.sub(r'\s*<path fill="#C81E1E"[^>]*/>', "", t))
    r = pixelcheck.check(gone, out / "clean-color.png")
    assert r["lost"] and r["missing_colors"] == ["#C81E1E"]


def test_diagonal_stripes_overlap_stays_narrow(tmp_path):
    # shared diagonal edges are traced twice (once per color) and can disagree by about a pixel
    a = np.zeros((300, 300, 4), np.uint8)
    yy, xx = np.mgrid[:300, :300]
    band = ((xx + yy) // 6) % 3
    for i, c in enumerate((RED, GREEN, BLUE)):
        a[band == i] = (*c, 255)
    img = tmp_path / "stripes.png"
    Image.fromarray(a, "RGBA").save(img)
    out, stem, _ = run_coaster(img, palette=posterize.parse_palette("C81E1E,0D5E1E,1E3CC8"),
                               speckle=1)
    r = assert_faithful(out, stem)
    assert r["overlap_width"] <= 1.5


# ---------------------------------------------------------------- automatic palette

def antialiased(draw, size, scale=4):
    big = Image.new("RGB", (size[0] * scale, size[1] * scale), "white")
    draw(ImageDraw.Draw(big), scale)
    return big.resize(size, Image.Resampling.LANCZOS)


# Fixtures from the third review: each has known ink colors, and the default automatic palette
# (--colors 4, what the app uses) must keep every one of them.

def outline(d):            # 2 px darker outline around a fill, plus a yellow dot
    d.rounded_rectangle((40, 40, 360, 260), 40, fill=(20, 20, 20))
    d.rounded_rectangle((42, 42, 358, 258), 38, fill=(30, 42, 80))
    d.ellipse((150, 100, 250, 200), fill=(20, 20, 20))
    d.ellipse((152, 102, 248, 198), fill=YELLOW)
    return [(30, 42, 80), (20, 20, 20), YELLOW]


def sticker(d):            # black 3 px sticker outline around a red shape with navy and yellow
    d.rounded_rectangle((30, 30, 370, 270), 50, fill=(20, 20, 20))
    d.rounded_rectangle((33, 33, 367, 267), 47, fill=(210, 40, 40))
    d.ellipse((90, 80, 210, 200), fill=(30, 42, 80))
    d.rectangle((240, 90, 330, 180), fill=YELLOW)
    return [YELLOW, (210, 40, 40), (30, 42, 80), (20, 20, 20)]


def thin_text(d):          # 2 px yellow strokes on an orange field
    d.rectangle((10, 10, 390, 290), fill=(245, 130, 48))
    for y in (40, 80, 120):
        d.line((30, y, 370, y), fill=YELLOW, width=2)
    for x in range(40, 360, 24):
        d.line((x, 160, x + 12, 260), fill=YELLOW, width=2)
    return [(245, 130, 48), YELLOW]


def pinstripe(d):          # orange 2 px line between red and yellow, a blend of both
    d.rectangle((10, 10, 390, 148), fill=(210, 30, 30))
    d.rectangle((10, 151, 390, 290), fill=(250, 220, 30))
    d.rectangle((10, 149, 390, 150), fill=(240, 120, 30))
    return [(210, 30, 30), (250, 220, 30), (240, 120, 30)]


def two_greens(d):         # two real greens about 20 RGB apart
    d.polygon([(200, 20), (380, 280), (20, 280)], fill=(46, 125, 50))
    d.polygon([(200, 20), (380, 280), (200, 280)], fill=(56, 142, 60))
    d.ellipse((170, 180, 230, 240), fill=YELLOW)
    return [(46, 125, 50), (56, 142, 60), YELLOW]


PALETTE_CASES = [outline, sticker, thin_text, pinstripe, two_greens]


def palette_case(tmp_path, draw, aa=False):
    im = Image.new("RGB", (400, 300), "white")
    colors = draw(ImageDraw.Draw(im))
    if aa:
        im = im.resize((1600, 1200), Image.Resampling.NEAREST).resize(
            (400, 300), Image.Resampling.LANCZOS)
    img = tmp_path / f"{draw.__name__}{'_aa' if aa else ''}.png"
    im.save(img)
    return img, colors


def kept_colors(out, stem, colors, tol=12):
    """The true colors that have a design path within TOL (RGB)."""
    fills = [tuple(int(p.get("fill")[i:i + 2], 16) for i in (1, 3, 5))
             for p in design_paths(out, stem)]
    return [c for c in colors if any(np.linalg.norm(np.subtract(c, f)) <= tol for f in fills)]


@pytest.mark.parametrize("draw", PALETTE_CASES, ids=lambda f: f.__name__)
@pytest.mark.parametrize("aa", [False, True], ids=["crisp", "antialiased"])
def test_default_auto_palette_keeps_every_color(tmp_path, draw, aa):
    img, colors = palette_case(tmp_path, draw, aa)
    logs = []
    out, stem, f = coaster.make_coaster(img, colors=4, log=logs.append)
    assert kept_colors(out, stem, colors) == colors
    assert f["parts"] >= len(colors)          # an anti-alias halo may add a part, never remove one
    assert not [m for m in logs if m.startswith(("folded", "merged"))]


def test_fold_halos_is_opt_in_and_logged(tmp_path):
    def badge(d, s):
        d.ellipse((20 * s, 20 * s, 280 * s, 280 * s), fill=GREEN)
        d.rectangle((90 * s, 130 * s, 210 * s, 170 * s), fill=YELLOW)
    img = tmp_path / "badge.png"
    antialiased(badge, (300, 300)).save(img)
    out, stem, f = run_coaster(img, palette=None, colors=4)
    assert f["parts"] == 3                        # default: the halo is a thin extra part
    logs = []
    out, stem, f = coaster.make_coaster(img, colors=4, fold_halos=True, out=tmp_path / "folded",
                                        log=logs.append)
    assert f["parts"] == 2
    folds = [m for m in logs if m.startswith("folded")]
    assert folds and all(re.fullmatch(
        r"folded #[0-9A-F]{6}, [\d,]+ px, into .+ \([\d,]+ px\)( and .+ \([\d,]+ px\))*", m)
        for m in folds), folds
    assert_faithful(out, stem)


def test_merge_close_is_opt_in_and_logged(tmp_path):
    rng = np.random.default_rng(3)
    a = np.full((200, 200, 3), 255, np.int16)
    a[40:160, 40:160] = RED
    a[40:160, 40:160] += rng.integers(-14, 15, (120, 120, 3))   # mottled, like a cheap JPEG
    img = tmp_path / "mottled.jpg"
    Image.fromarray(np.clip(a, 0, 255).astype(np.uint8)).save(img, quality=60)
    out, stem, f = run_coaster(img, palette=None, colors=4)
    assert f["parts"] > 1                        # default keeps every cluster
    logs = []
    out, stem, f = coaster.make_coaster(img, colors=4, merge_close=24, fold_halos=True,
                                        out=tmp_path / "merged", log=logs.append)
    assert f["parts"] == 1
    merges = [m for m in logs if m.startswith("merged")]
    assert merges and all(re.fullmatch(r"merged #[0-9A-F]{6}, [\d,]+ px, into #[0-9A-F]{6}", m)
                          for m in merges)


def test_fold_and_merge_refuse_an_explicit_palette(tmp_path):
    img, _ = palette_case(tmp_path, outline)
    for kw in ({"fold_halos": True}, {"merge_close": 24}):
        with pytest.raises(ValueError, match="automatic palette"):
            run_coaster(img, palette=posterize.parse_palette("1E2A50,141414,F6B820"), **kw)


def test_explicit_palette_keeps_a_thin_line(tmp_path):
    im = Image.new("RGB", (100, 60), "white")
    d = ImageDraw.Draw(im)
    d.rectangle((10, 10, 90, 50), fill=YELLOW)
    d.rectangle((10, 10, 90, 11), fill=RED)            # a 2 px red line along the top
    img = tmp_path / "line.png"
    im.save(img)
    out, stem, f = run_coaster(img, palette=posterize.parse_palette("F6B820,C81E1E"), speckle=1)
    assert f["parts"] == 2


# ---------------------------------------------------------------- zero-area subpaths

def test_zero_area_subpaths_are_dropped(tmp_path):
    assert finalize._drop_flat_subpaths("M0,0 L4,0 L4,4 L0,4 Z M2,2 Z M1,1 L3,3 Z") == "M0,0 L4,0 L4,4 L0,4 Z"
    assert finalize._drop_flat_subpaths("M2,2 Z") == ""
    img = tiny_counters(tmp_path / "counters.png")    # its 1 px counter traces as "M x,y Z"
    out, stem, _ = run_coaster(img, palette=posterize.parse_palette("0D5E1E"))
    assert re.search(r'M[-\d.]+,[-\d.]+ Z', (out / "raw.svg").read_text())   # vtracer wrote one
    for p in design_paths(out, stem):
        for sub in filter(None, re.split(r'(?=M)', p.get("d").strip())):
            assert len(re.findall(r'-?\d*\.?\d+', sub)) >= 6, sub


# ---------------------------------------------------------------- photos are refused early

def watched(cmd, *, max_gb, max_secs):
    """Run CMD, killing it above MAX_GB resident or MAX_SECS. Returns (rc, stderr, peak_gb, secs)."""
    import subprocess
    import time
    t0 = time.time()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    peak = 0.0
    while proc.poll() is None:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(proc.pid)], capture_output=True,
                             text=True).stdout.strip()
        peak = max(peak, int(out or 0) / 2**20)
        if peak > max_gb or time.time() - t0 > max_secs:
            proc.kill()
            proc.wait()
            pytest.fail(f"over budget: {peak:.2f} GB, {time.time() - t0:.0f} s")
        time.sleep(0.05)
    return proc.returncode, proc.stderr.read(), peak, time.time() - t0


def test_noisy_megapixel_image_finishes_within_budget(tmp_path):
    # stacked tracing is linear in the image: 1 MP of noise finishes in seconds, not hours
    rng = np.random.default_rng(0)
    img = tmp_path / "noise.png"
    Image.fromarray(rng.integers(0, 256, (1000, 1000, 3), dtype=np.uint8)).save(img)
    rc, err, peak, secs = watched([sys.executable, "-P", "-m", "marquetry_svg", "coaster",
                                   str(img), "--colors", "4"], max_gb=1.5, max_secs=60)
    assert rc == 0, err
    assert peak < 1.0 and secs < 30


def hatch(path, size, pitch=6):
    """Engraved-seal cross-hatching: every cell is its own background region."""
    im = Image.new("RGB", (size, size), "white")
    d = ImageDraw.Draw(im)
    for v in range(-size, 2 * size, pitch):
        d.line((v, 0, v + size, size), fill=(25, 25, 25), width=2)
        d.line((v, size, v + size, 0), fill=(25, 25, 25), width=2)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((size // 25, size // 25, size - size // 25, size - size // 25), fill=255)
    base = Image.new("RGB", (size, size), "white")
    ImageDraw.Draw(base).ellipse((size // 100, size // 100, size - size // 100, size - size // 100),
                                 outline=(25, 25, 25), width=max(2, size // 80))
    base.paste(im, (0, 0), mask)
    base.save(path)
    return path


def test_hatched_seal_passes_the_region_bound(tmp_path):
    img = hatch(tmp_path / "seal.png", 1000)               # ~37,000 regions at 1 MP
    rc, err, peak, secs = watched([sys.executable, "-P", "-m", "marquetry_svg", "coaster",
                                   str(img), "--colors", "4"], max_gb=1.5, max_secs=60)
    assert rc == 0, err
    assert peak < 1.0 and secs < 30


def test_extreme_region_count_is_refused_and_can_be_overridden(tmp_path):
    rng = np.random.default_rng(1)
    img = tmp_path / "noise.png"
    Image.fromarray(rng.integers(0, 256, (1100, 1100, 3), dtype=np.uint8)).save(img)
    rc, err, peak, secs = watched([sys.executable, "-P", "-m", "marquetry_svg", "coaster",
                                   str(img), "--colors", "8", "--speckle", "1"],
                                  max_gb=1.5, max_secs=60)
    assert rc == 2 and "separate regions" in err and "--max-regions" in err, err
    assert "--speckle" not in err                           # it can't help hatching
    assert secs < 30 and not (tmp_path / "noise.png-coaster").exists()
    small = hatch(tmp_path / "small.png", 300)
    with pytest.raises(ValueError, match="--max-regions"):
        run_coaster(small, palette=None, colors=2, max_regions=1000)
    out, stem, f = run_coaster(small, palette=None, colors=2, max_regions=0, out=tmp_path / "any")
    assert f["parts"] >= 1
    with pytest.raises(ValueError, match="--max-regions"):
        run_coaster(small, palette=None, colors=2, max_regions=-1)


def test_paths_too_long_for_qt_still_preview_and_check(tmp_path):
    # Qt draws nothing for a path over ~32,000 commands; busy art gets far past that
    img = hatch(tmp_path / "seal600.png", 600)
    out, stem, f = run_coaster(img, palette=None, colors=2, make_preview=True)
    d = design_paths(out, stem)[0].get("d")
    assert len(re.findall(r"[MLZ]", d)) > preview.QT_PATH_LIMIT
    shown = np.asarray(Image.open(out / "preview-design.png").convert("RGB")).astype(int)
    ink = np.asarray(Image.open(out / "clean-color.png").convert("RGBA"))[..., 3] >= 128
    not_magenta = np.abs(shown - (255, 0, 255)).sum(-1) > 60
    # not blank: the 500 px preview of 600 px hatching loses a little of each 2 px line
    assert abs(not_magenta.mean() - ink.mean()) < 0.1, (not_magenta.mean(), ink.mean())
    assert_faithful(out, stem)
