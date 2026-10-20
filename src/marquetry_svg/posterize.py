"""Step 1: posterize a flat logo into a small fixed palette.

Tracing a JPEG directly is a trap: JPEG noise and anti-aliased edges create hundreds of
near-duplicate shades, and each becomes its own micro-path. Quantizing FIRST collapses the
image to N clean regions, one filament each.

What it does:
  1. Load the image upright (EXIF orientation applied).
  2. Split background from ink. Background is any pixel that is transparent (alpha < 128) or
     light AND unsaturated (white to light grey). Background becomes holes in the design, so
     the base shows through.
  3. Use the explicit palette, or KMeans the ink pixels into N colors.
  4. Snap every ink pixel to its nearest palette color. The median despeckle is OFF by
     default: median >= 3 closes thin letter counters (the holes in A/o/e).
  5. Drop ink specks: connected ink regions smaller than speckle x speckle pixels take the
     color around them. Background regions (counters) are never filled.
  6. Write clean-color.png (palette on a transparent background), clean-emboss.png (all ink
     one dark color), clean-on-white.png (sanity preview) and palette.json.
"""
import io
import json
import re
import warnings
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

from . import fsio

MAX_COLORS = 32
MAX_SPECKLE = 64
KMEANS_SAMPLE = 250_000    # KMeans fits on at most this many ink pixels (fixed seed)
CHUNK = 1 << 20            # pixels per block when snapping to the palette
REGIONS_PER_MP = 250_000   # default --max-regions: separate regions per megapixel (sanity bound)
HALO_SAME = 24.0           # --fold-halos: a blend is within this RGB distance of the line
HALO_SHADE = 72.0          # --fold-halos: a shade is within this RGB distance of a neighbor


def parse_hex(text, what="color"):
    """'#F6B820' or 'f6b820' -> '#F6B820'. Raises ValueError otherwise."""
    h = str(text).strip().lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", h):
        raise ValueError(f"{what} must be a 6-digit hex color like F6B820, got {text!r}")
    return "#" + h.upper()


def parse_palette(text):
    """'F6B820,#0D5E1E, AD6D1A' -> [(246, 184, 32), ...]. Raises ValueError on bad hex."""
    palette = []
    for h in text.replace(" ", "").split(","):
        if not h:
            continue
        h = parse_hex(h, "palette colors")
        palette.append(tuple(int(h[i:i + 2], 16) for i in (1, 3, 5)))
    if not palette:
        raise ValueError("empty palette")
    if len(palette) > MAX_COLORS:
        raise ValueError(f"at most {MAX_COLORS} palette colors")
    return palette


def check_settings(*, colors=4, speckle=4, median=1):
    if not 1 <= colors <= MAX_COLORS:
        raise ValueError(f"--colors must be between 1 and {MAX_COLORS}, got {colors}")
    if not 0 <= speckle <= MAX_SPECKLE:
        raise ValueError(f"--speckle must be between 0 and {MAX_SPECKLE}, got {speckle}")
    if not 0 <= median <= 15:
        raise ValueError(f"--median must be between 0 and 15, got {median}")


def load_image(path):
    """Open PATH upright (EXIF orientation applied) as an RGBA uint8 array, H x W x 4."""
    try:
        with Image.open(path) as src:
            im = ImageOps.exif_transpose(src)
            if im.mode == "I" or im.mode.startswith("I;16"):
                a = np.asarray(im).astype(np.float64)
                if a.max() > 255:      # 16-bit greyscale
                    a = a / 257.0
                im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8), "L")
            rgba = np.asarray(im.convert("RGBA"))
    except FileNotFoundError:
        raise FileNotFoundError(f"no such image: {path}") from None
    except Image.DecompressionBombError as e:
        raise ValueError(f"{path} is too large to process safely ({e})") from None
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as e:
        raise ValueError(f"cannot read {path} as an image: {e}") from None
    if rgba.shape[0] < 1 or rgba.shape[1] < 1:
        raise ValueError(f"{path} has no pixels")
    return rgba


def background_mask(rgba, val_bg=0.80, sat_bg=0.20):
    rgb = rgba[..., :3].astype(np.float32)
    mx = rgb.max(-1)
    mn = rgb.min(-1)
    val = mx / 255.0
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0)
    return ((val > val_bg) & (sat < sat_bg)) | (rgba[..., 3] < 128)


def nearest(flat, palette):
    """Index of the nearest palette color for each row of FLAT (N x 3), in blocks."""
    pal = np.asarray(palette, np.float32)
    idx = np.empty(len(flat), np.int32)
    for i in range(0, len(flat), CHUNK):
        blk = flat[i:i + CHUNK]
        idx[i:i + CHUNK] = ((blk[:, None, :] - pal[None]) ** 2).sum(2).argmin(1)
    return idx


def drop_specks(cls, n_ink, min_area):
    """Give each 8-connected ink region smaller than MIN_AREA pixels the most common class on
    its border (another ink, or background). Background regions are left alone, so small
    counters stay open."""
    if min_area <= 1:
        return cls
    from scipy import ndimage
    eight = np.ones((3, 3), bool)
    for c in range(n_ink):
        lab, n = ndimage.label(cls == c, structure=eight)
        if n == 0:
            continue
        sizes = np.bincount(lab.ravel())
        small = [k for k in np.flatnonzero(sizes < min_area) if k != 0]
        if not small:
            continue
        objs = ndimage.find_objects(lab)
        for k in small:
            sl = tuple(slice(max(s.start - 1, 0), s.stop + 1) for s in objs[k - 1])
            region = lab[sl] == k
            ring = ndimage.binary_dilation(region, structure=eight) & ~region
            around = cls[sl][ring]
            around = around[around != c]
            cls[sl][region] = np.bincount(around).argmax() if around.size else n_ink
    return cls


def _near_segment(c, a, b):
    """Distance from color C to the segment between colors A and B."""
    c, a, b = (np.asarray(x, np.float32) for x in (c, a, b))
    ab = b - a
    t = 0.0 if not ab.any() else float(np.clip(np.dot(c - a, ab) / np.dot(ab, ab), 0, 1))
    return float(np.linalg.norm(c - (a + t * ab)))


def hexc(rgb):
    return "#%02X%02X%02X" % tuple(int(v) for v in rgb)


def merge_thin_colors(cls, palette, bg_rgb, log=None):
    """--fold-halos: fold colors that look like edge artifacts into the colors next to them.

    A color is folded when it is (almost) all thin edges, under 5% of its pixels lying more
    than 1 px inside it, so regions 2 px wide or less with at most a few thicker specks, AND its
    color looks like an edge: a blend of two colors it touches (anti-aliasing, within
    HALO_SAME of the line between them) or a shade of one (JPEG ringing, within HALO_SHADE).
    Each folded pixel takes the class of the nearest pixel outside the folded colors. Every
    fold is logged. Opt-in: it can also fold a real thin outline or thin text whose color
    happens to sit between or near its neighbors. Returns (class map, indices kept)."""
    from scipy import ndimage
    n_ink = len(palette)
    three = np.ones((3, 3), bool)
    colors = list(palette) + [bg_rgb]          # index n_ink is the background
    fold = []
    for c in range(n_ink):
        m = cls == c
        if not m.any() or ndimage.binary_erosion(m, structure=three).sum() >= 0.05 * m.sum():
            continue
        ring = ndimage.binary_dilation(m, structure=three) & ~m
        near = [int(k) for k in np.argsort(-np.bincount(cls[ring], minlength=n_ink + 1))[:3]
                if (cls[ring] == k).any()]
        blend = any(_near_segment(palette[c], colors[a], colors[b]) < HALO_SAME
                    for i, a in enumerate(near) for b in near[i + 1:])
        shade = any(a < n_ink and np.linalg.norm(np.subtract(palette[c], colors[a], dtype=np.float32))
                    < HALO_SHADE for a in near)
        if blend or shade:
            fold.append(c)
    keep = [c for c in range(n_ink) if c not in fold]
    if not fold or not any((cls == c).any() for c in keep):
        return cls, list(range(n_ink))
    mask = np.isin(cls, fold)
    _, (iy, ix) = ndimage.distance_transform_edt(mask, return_indices=True)
    new = cls.copy()
    new[mask] = cls[iy[mask], ix[mask]]
    if log:
        for c in fold:
            m = cls == c
            into = np.bincount(new[m], minlength=n_ink + 1)
            parts = [f"{'the background' if t == n_ink else hexc(palette[t])} ({into[t]:,} px)"
                     for t in np.argsort(-into) if into[t]]
            log(f"folded {hexc(palette[c])}, {int(m.sum()):,} px, into " + " and ".join(parts))
    cls = new
    remap = np.full(n_ink + 1, len(keep), cls.dtype)   # background stays one past the end
    for new, old in enumerate(keep):
        remap[old] = new
    return remap[cls], keep


def count_regions(cls):
    """Number of 8-connected regions of every class in CLS (colors and background)."""
    from scipy import ndimage
    eight = np.ones((3, 3), bool)
    return sum(ndimage.label(cls == c, structure=eight)[1] for c in np.unique(cls))


def region_limit(shape, max_regions=None):
    """The --max-regions bound for an image of SHAPE: None means the default, which scales with
    the image (REGIONS_PER_MP, at least 50,000); 0 means no limit."""
    if max_regions is None:
        return max(50_000, int(REGIONS_PER_MP * shape[0] * shape[1] / 1e6))
    if max_regions < 0:
        raise ValueError(f"--max-regions must be 0 (no limit) or more, got {max_regions}")
    return max_regions


def check_regions(cls, max_regions=None):
    """A sanity bound against very long runs, not a photo detector: busy line art such as
    hatching legitimately has tens of thousands of regions."""
    limit = region_limit(cls.shape, max_regions)
    if not limit:
        return
    n = count_regions(cls)
    if n > limit:
        raise ValueError(f"found {n:,} separate regions (limit {limit:,}); this looks more like a "
                         "photo or noise than flat artwork, and tracing it would take a long time. "
                         "If it really is busy artwork such as hatching, rerun with a higher "
                         "--max-regions, or --max-regions 0 for no limit")


def quantize(rgba, *, colors=4, palette=None, val_bg=0.80, sat_bg=0.20, median=1, speckle=4,
             fold_halos=False, merge_close=0.0, max_regions=None, log=None):
    """Posterize an RGBA array in memory. Returns (palette, class map); class len(palette) is
    background. Raises ValueError before anything is written when there is too little ink.

    By default every color of the palette is kept. FOLD_HALOS and MERGE_CLOSE (automatic
    palette only) remove colors, and every removal is passed to LOG."""
    check_settings(colors=colors if not palette else 1, speckle=speckle, median=median)
    if palette and (fold_halos or merge_close):
        raise ValueError("--fold-halos and --merge-close only apply to the automatic palette "
                         "(--colors), not to --palette")
    if not 0 <= merge_close <= 442:
        raise ValueError(f"--merge-close must be between 0 and 442 (RGB distance), got {merge_close}")
    region_limit(rgba.shape, max_regions)
    H, W = rgba.shape[:2]
    flat = rgba[..., :3].reshape(-1, 3).astype(np.float32)
    ink = ~background_mask(rgba, val_bg, sat_bg).reshape(-1)
    n_colors = len(palette) if palette else colors
    if ink.sum() < n_colors:
        raise ValueError(f"found {int(ink.sum())} ink pixels for {n_colors} colors; the image "
                         "looks like all background (adjust --val-bg/--sat-bg, or use fewer colors)")

    auto = not palette
    if palette:
        palette = [tuple(int(v) for v in c) for c in palette]
    else:
        from sklearn.cluster import KMeans
        from sklearn.exceptions import ConvergenceWarning
        pts = flat[ink]
        if len(pts) > KMEANS_SAMPLE:
            pts = pts[np.random.default_rng(0).choice(len(pts), KMEANS_SAMPLE, replace=False)]
        with warnings.catch_warnings():
            # fewer distinct inks than --colors: duplicate centers are merged below
            warnings.simplefilter("ignore", ConvergenceWarning)
            km = KMeans(n_clusters=colors, n_init=10, random_state=0).fit(pts)
        palette = list(dict.fromkeys(tuple(int(round(v)) for v in c) for c in km.cluster_centers_))
        if merge_close:
            palette = _merge_close(palette, flat[ink], merge_close, log)

    BG = len(palette)   # one past the last palette index, so no ink color is mistaken for background
    cls = np.where(ink, nearest(flat, palette), BG).reshape(H, W)
    if median and median > 1:
        from scipy.ndimage import median_filter
        cls = median_filter(cls, size=median)
    if auto and fold_halos:
        bg = background_mask(rgba, val_bg, sat_bg) & (rgba[..., 3] >= 128)
        bg_rgb = tuple(np.median(rgba[..., :3][bg], axis=0)) if bg.any() else (255, 255, 255)
        cls, kept = merge_thin_colors(cls, palette, bg_rgb, log)
        palette = [palette[i] for i in kept]
        BG = len(palette)
    cls = drop_specks(cls, BG, speckle * speckle)
    if not (cls < BG).any():
        raise ValueError(f"no ink left after the speck filter; every ink region is smaller than "
                         f"{speckle}x{speckle} pixels (pass --speckle 1 for tiny images)")
    check_regions(cls, max_regions)
    return palette, cls


def _merge_close(palette, pts, dist, log):
    """--merge-close: keep the biggest of each group of colors closer than DIST (RGB) and
    log every merge."""
    counts = np.bincount(nearest(pts, palette), minlength=len(palette))
    kept = []
    for i in np.argsort(-counts, kind="stable"):
        c = np.asarray(palette[i], np.float32)
        near = [k for k in kept if np.linalg.norm(c - np.asarray(palette[k], np.float32)) < dist]
        if near:
            if log:
                log(f"merged {hexc(palette[i])}, {int(counts[i]):,} px, into {hexc(palette[near[0]])}")
        else:
            kept.append(i)
    return [palette[i] for i in sorted(kept)]


def color_image(palette, cls):
    """The posterized image: palette colors on a transparent background (RGBA array)."""
    color = np.zeros(cls.shape + (4,), np.uint8)
    for i, rgb in enumerate(palette):
        color[cls == i] = (*rgb, 255)
    return color


def _png(arr, mode):
    buf = io.BytesIO()
    Image.fromarray(arr, mode).save(buf, format="PNG")
    return buf.getvalue()


def outputs(palette, cls):
    """({name: bytes} for clean-color/-emboss/-on-white.png and palette.json, summary dict)."""
    H, W = cls.shape
    BG = len(palette)
    white = np.full((H, W, 3), 255, np.uint8)
    for i, rgb in enumerate(palette):
        white[cls == i] = rgb
    emboss = np.zeros((H, W, 4), np.uint8)
    emboss[cls < BG] = (26, 26, 26, 255)
    files = {
        "clean-color.png": _png(color_image(palette, cls), "RGBA"),
        "clean-emboss.png": _png(emboss, "RGBA"),
        "clean-on-white.png": _png(white, "RGB"),
        "palette.json": json.dumps({"palette": [list(p) for p in palette], "size": [W, H]},
                                   indent=2).encode(),
    }
    counts = np.bincount(cls.ravel(), minlength=BG + 1)[:BG]
    tot = max(int(counts.sum()), 1)
    return files, {"palette": palette, "shares": [100 * int(n) / tot for n in counts],
                   "ink": float(counts.sum() / (W * H)), "size": [W, H]}


def posterize(image, outdir, **settings):
    """Posterize IMAGE into OUTDIR (see quantize for SETTINGS). Returns {"palette", "shares"
    (percent of ink per color), "ink" (fraction of the image), "size" [W, H]}."""
    palette, cls = quantize(load_image(image), **settings)
    files, summary = outputs(palette, cls)
    with fsio.OutDir(outdir, explicit=True) as od:
        od.replace_all(files, protect=fsio.identities(image))
    return summary


def add_arguments(ap):
    ap.add_argument("image")
    ap.add_argument("outdir")
    ap.add_argument("--colors", type=int, default=4,
                    help="number of ink colors for auto KMeans, not counting the background")
    ap.add_argument("--palette", default="",
                    help="explicit hex colors, e.g. F6B820,0D5E1E,AD6D1A; bypasses KMeans. "
                         "Preferred: auto-clustering can merge a real color into an anti-alias halo")
    ap.add_argument("--val-bg", type=float, default=0.80,
                    help="light pixels count as background when brightness > this (0-1); "
                         "pass 1 to keep white as a color")
    ap.add_argument("--sat-bg", type=float, default=0.20,
                    help="...and saturation < this (0-1)")
    ap.add_argument("--speckle", type=int, default=4,
                    help="ink specks smaller than N x N pixels take the surrounding color "
                         "(0 or 1 keeps everything)")
    ap.add_argument("--median", type=int, default=1,
                    help="despeckle kernel (odd; below 2 is off). Keep off: 3 or more closes thin "
                         "letter counters")
    add_palette_options(ap)


def add_palette_options(ap):
    ap.add_argument("--fold-halos", action="store_true",
                    help="automatic palette: fold colors that are only thin edges blending into "
                         "or shading their neighbors (anti-alias and JPEG halos) into those "
                         "neighbors; each fold is logged. Can also fold a real thin outline")
    ap.add_argument("--merge-close", type=float, default=0.0, metavar="RGB",
                    help="automatic palette: merge colors closer than this RGB distance "
                         "(e.g. 24, for JPEG noise splitting one color); each merge is logged")
    ap.add_argument("--max-regions", type=int, default=None, metavar="N",
                    help=f"refuse images that break into more than N separate regions (default "
                         f"{REGIONS_PER_MP:,} per megapixel, at least 50,000; 0 = no limit)")


def run(a):
    palette = parse_palette(a.palette) if a.palette.strip() else None
    r = posterize(a.image, a.outdir, colors=a.colors, palette=palette, val_bg=a.val_bg,
                  sat_bg=a.sat_bg, median=a.median, speckle=a.speckle, fold_halos=a.fold_halos,
                  merge_close=a.merge_close, max_regions=a.max_regions, log=print)
    W, H = r["size"]
    print(f"size {W}x{H}  ink {100 * r['ink']:.1f}%  colors={len(r['palette'])}"
          f"{' (explicit)' if palette else ' (auto KMeans)'}")
    for rgb, share in zip(r["palette"], r["shares"]):
        print(f"  #{rgb[0]:02X}{rgb[1]:02X}{rgb[2]:02X}  {share:5.1f}% of ink")
    print(f"wrote {a.outdir}/clean-color.png, clean-emboss.png, clean-on-white.png, palette.json")
    print("next: marquetry trace on clean-color.png, then marquetry finalize "
          "(or run marquetry coaster for all three steps)")
