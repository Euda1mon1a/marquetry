"""Compare a design SVG with the posterized image it was traced from.

Each <path> is rasterized on its own at K times the image's size (alpha >= 128 counts as
covered), and compared with the posterized image scaled up the same way. Nothing is masked
off at color boundaries; instead the check bounds how WIDE any disagreement is, in source
pixels, and how much each color's area drifts:

  overlap_width  widest strip covered by two colors (parts overlapping in the slicer)
  gap_width      widest strip of ink covered by no color
  leak_width     widest strip of background covered by a color
  wrong_width    widest strip of ink covered only by another color
  area_drift     per color, |rendered area - posterized area| / that color's perimeter
  lost           posterized regions at least 2 px wide that their color covers < 50% of

A 1 px dilation of every path, for instance, shows up as a 2 px overlap and an area drift
near 1, and a small dot that vanished shows up in lost.
"""
import io
import re
import xml.etree.ElementTree as ET   # our own generated SVGs, not untrusted input

import numpy as np
from PIL import Image
from scipy import ndimage

from marquetry_svg import preview

SVG = "{http://www.w3.org/2000/svg}"
K = 4


def path_masks(svg_text, k=K):
    """{fill: bool mask at k x size}, each path rasterized alone."""
    root = ET.fromstring(svg_text)
    W, H = int(root.get("width")), int(root.get("height"))
    head = re.match(r'(?s).*?<svg[^>]*>', svg_text).group(0)
    masks = {}
    for p in root.findall(f"{SVG}path"):
        attrs = " ".join(f'{a}="{v}"' for a, v in p.attrib.items())
        png = preview.render(f"{head}<path {attrs}/></svg>", W * k, H * k)
        m = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))[..., 3] >= 128
        fill = p.get("fill")
        masks[fill] = masks.get(fill, np.zeros_like(m)) | m
    return masks, (W, H)


def expected_classes(clean_color_png):
    """{fill: bool mask} of the posterized ink at 1x."""
    a = np.asarray(Image.open(clean_color_png).convert("RGBA"))
    ink = a[..., 3] >= 128
    return {"#%02X%02X%02X" % rgb: ink & np.all(a[..., :3] == rgb, axis=-1)
            for rgb in {tuple(c) for c in a[ink][:, :3]}}, ~ink


def _width(mask, k):
    """Width in source pixels of the widest strip in MASK (a k x mask)."""
    if not mask.any():
        return 0.0
    return 2 * float(ndimage.distance_transform_edt(mask).max()) / k


def _up(mask, k):
    return np.repeat(np.repeat(mask, k, 0), k, 1)


def _perimeter(mask):
    m = mask.astype(np.int8)
    return int(np.abs(np.diff(m, axis=0)).sum() + np.abs(np.diff(m, axis=1)).sum()
               + m[0].sum() + m[-1].sum() + m[:, 0].sum() + m[:, -1].sum())


def check(design_svg, clean_color_png, k=K):
    got, (W, H) = path_masks(open(design_svg).read(), k)
    want, bg = expected_classes(clean_color_png)
    zero = np.zeros((H * k, W * k), bool)
    count = sum(m.astype(np.int8) for m in got.values()) if got else zero.astype(np.int8)
    ink_k = _up(~bg, k)
    res = {
        "overlap_width": _width(count > 1, k),
        "overlap_area": float((count > 1).sum()) / k / k,
        "gap_width": _width(ink_k & (count == 0), k),
        "leak_width": _width(~ink_k & (count > 0), k),
        # a color whose regions are all under 2 px wide may legitimately not trace
        "missing_colors": sorted(f for f in set(want) - set(got)
                                 if ndimage.binary_erosion(want[f], np.ones((2, 2), bool)).any()),
        "extra_colors": sorted(set(got) - set(want)),
        "area_drift": {}, "lost": [],
    }
    wrong = zero.copy()
    for fill, m in want.items():
        g = got.get(fill, zero)
        mk = _up(m, k)
        wrong |= mk & ~g & (count > 0)
        drift = abs(g.sum() / k / k - m.sum()) / max(_perimeter(m), 1)
        res["area_drift"][fill] = round(float(drift), 3)
        # regions at least 2 px wide must be mostly covered by their own color
        lab, n = ndimage.label(m, structure=np.ones((3, 3), bool))
        if n:
            frac = g.reshape(H, k, W, k).mean(axis=(1, 3))
            cover = ndimage.mean(frac, lab, np.arange(1, n + 1))
            wide = ndimage.maximum(ndimage.binary_erosion(m, np.ones((2, 2), bool)), lab,
                                   np.arange(1, n + 1))
            for i in np.flatnonzero((np.asarray(wide) > 0) & (np.asarray(cover) < 0.5)):
                ys, xs = np.nonzero(lab == i + 1)
                res["lost"].append((fill, int(xs.mean()), int(ys.mean()), round(float(cover[i]), 2)))
    res["wrong_width"] = _width(wrong, k)
    res["max_area_drift"] = max(res["area_drift"].values(), default=0.0)
    return res


# The bounds the README states: where two colors share a curved or diagonal edge their parts
# can overlap by up to about 1.5 px, and outlines can miss or overshoot the posterized image by
# up to about 1.5 px at corners. Across 47 review fixtures the worst seen was overlap 1.41,
# gap 1.58, leak 1.50, drift 0.32; a 1 px dilation of every path gives leak ~2 and drift ~1.
LIMITS = {"overlap_width": 1.6, "gap_width": 1.6, "leak_width": 1.6, "wrong_width": 1.6,
          "max_area_drift": 0.5}


def problems(res):
    out = [f"{key} {res[key]:.2f} > {lim}" for key, lim in LIMITS.items() if res[key] > lim]
    if res["missing_colors"]:
        out.append(f"missing colors {res['missing_colors']}")
    if res["extra_colors"]:
        out.append(f"extra colors {res['extra_colors']}")
    if res["lost"]:
        out.append(f"lost regions {res['lost'][:5]}")
    return out
