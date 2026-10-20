import os

import pytest
from PIL import Image, ImageDraw

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

YELLOW = (246, 184, 32)
GREEN = (13, 94, 30)
BROWN = (173, 109, 26)
RED = (200, 30, 30)
BLUE = (30, 60, 200)
PALETTE = "F6B820,0D5E1E,AD6D1A"
RING_CENTER = (120, 160)   # center of the green "O"; its counter must stay a hole


def hexes(*rgbs):
    return {"#%02X%02X%02X" % c for c in rgbs}


def draw_logo(path, *, bg="white", mode="RGB"):
    """A 400x320 three-color logo: a yellow block, a green ring shaped like the letter O (the
    counter is background, so it must come out as a hole), a brown bar, and a thin green ring
    with a 6 px counter."""
    im = Image.new(mode, (400, 320), bg)
    d = ImageDraw.Draw(im)
    d.rectangle((40, 40, 360, 90), fill=YELLOW)
    cx, cy = RING_CENTER
    d.ellipse((cx - 60, cy - 60, cx + 60, cy + 60), fill=GREEN)
    d.ellipse((cx - 30, cy - 30, cx + 30, cy + 30), fill=bg)
    d.rectangle((220, 120, 360, 260), fill=BROWN)
    d.ellipse((290, 270, 310, 290), fill=GREEN)
    d.ellipse((297, 277, 303, 283), fill=bg)
    im.save(path)
    return path


@pytest.fixture
def logo(tmp_path):
    return draw_logo(tmp_path / "test logo.png")
