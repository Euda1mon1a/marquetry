"""Step 3: clean and CONSOLIDATE a vtracer trace into print-ready SVGs.

Every same-color path is merged into ONE <path>, so the slicer sees N parts (one filament
each), not dozens.

vtracer behaviour handled here:
  * Paths are RELATIVE ('M0 0 ...') and positioned by transform='translate(tx,ty)'. To merge
    many paths into one <path>, the translate is BAKED into absolute coordinates (a per-subpath
    transform can't live inside one path element). Baking relies on vtracer emitting only
    absolute M/L/C/Q/Z (even-count coordinate runs) so the global x/y parity holds; a path with
    H/V/A or relative commands is left un-merged rather than mis-baked.
  * Traced from an OPAQUE image, vtracer paints the whole canvas with a background region.
    --drop-bg detects that color (the fill of a path covering 90% of the canvas) and drops it.
    It is off by default: after marquetry posterize the background is already transparent, and
    on such a trace the only full-canvas path is real ink (a badge cropped to its edge).
    --speck-area (also off by default) drops small paths touching the image edge, debris from
    an opaque trace; posterize's speck filter covers this for its own output.
  * Subpaths that enclose no area ('M12,12 Z', a collapsed one-pixel hole) are removed.

Outputs (next to --out-prefix):
  -design.svg  : N consolidated color paths, transparent, NO base (emboss thin on top, so only
                 the top few layers are multi-color and the slice is fast)
  -emboss.svg  : the same outline as one single-color path (one-filament raise or deboss)
  -disc.svg    : base disc only           (only if base != none)
  -coaster.svg : base disc + design       (only if base != none; single import)
"""
import json
import re
from pathlib import Path

from . import fsio
from .posterize import parse_hex

BASES = ("circle", "square", "none")
SPECK_FRACTION = 0.002   # edge specks: at most this fraction of the canvas area


def _hex(rgb):
    return "#%02X%02X%02X" % tuple(rgb)


def _rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _parse(path):
    dm = re.search(r'\bd="([^"]+)"', path)
    fm = re.search(r'fill="(#[0-9a-fA-F]{6}|rgb\([^)]*\))"', path)
    if not dm or not fm:
        return None
    tm = re.search(r'transform="translate\(([-\d.]+)[ ,]+([-\d.]+)\)"', path)
    tx, ty = (float(tm.group(1)), float(tm.group(2))) if tm else (0.0, 0.0)
    fill = fm.group(1)
    if fill.startswith("rgb"):
        r, g, b = (min(int(v), 255) for v in re.findall(r'\d+', fill)[:3])
        fill = _hex((r, g, b))
    return dm.group(1), fill.upper(), tx, ty


def _bbox(d, tx, ty):
    n = [float(x) for x in re.findall(r'-?\d*\.?\d+', d)]
    xs = [v + tx for v in n[0::2]]
    ys = [v + ty for v in n[1::2]]
    return (min(xs), min(ys), max(xs), max(ys)) if xs and ys else None


def _bake(d, tx, ty):
    """Add (tx,ty) to every absolute coordinate. Returns None if the path uses
    commands that break simple x/y parity (relative cmds, or H/V/A)."""
    if re.search(r'[a-y]', d) or re.search(r'[HVA]', d):
        return None
    k = [0]

    def repl(m):
        v = float(m.group(0)) + (tx if k[0] % 2 == 0 else ty)
        k[0] += 1
        return f"{v:.2f}"
    return re.sub(r'-?\d*\.?\d+', repl, d)


def _drop_flat_subpaths(d):
    """Remove subpaths that enclose no area: vtracer writes 'M12,12 Z' for a collapsed hole."""
    kept = []
    for part in re.split(r'(?=M)', d.strip()):
        nums = [float(x) for x in re.findall(r'-?\d*\.?\d+', part)]
        xs, ys = nums[0::2], nums[1::2]
        n = min(len(xs), len(ys))
        area = abs(sum(xs[i] * ys[(i + 1) % n] - xs[(i + 1) % n] * ys[i] for i in range(n))) / 2
        if n >= 3 and area > 1e-6:
            kept.append(part.strip())
    return " ".join(kept)


def load_palette(path):
    """Read palette.json. Returns (palette, (W, H) or (None, None)); ValueError if malformed."""
    try:
        pj = json.loads(Path(path).read_text())
        palette = [tuple(int(v) for v in c) for c in pj["palette"]]
        size = pj.get("size") or [None, None]
        W, H = (None, None) if size == [None, None] else (int(size[0]), int(size[1]))
    except FileNotFoundError:
        raise FileNotFoundError(f"no such palette file: {path}") from None
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError) as e:
        raise ValueError(f"{path} is not a palette.json from marquetry posterize ({e!r})") from None
    if not palette or any(len(c) != 3 or not all(0 <= v <= 255 for v in c) for c in palette):
        raise ValueError(f"{path}: palette must be a list of [R, G, B] values from 0 to 255")
    if W is not None and (W < 1 or H < 1):
        raise ValueError(f"{path}: size must be positive")
    return palette, (W, H)


def check_options(base="circle", base_color="#FFFFFF", emboss_fill="#1A1A1A"):
    if base not in BASES:
        raise ValueError(f"base must be one of {BASES}")
    return base, parse_hex(base_color, "--base-color"), parse_hex(emboss_fill, "--emboss-fill")


def build(s, palette, size=(None, None), *, base="circle", base_color="#FFFFFF",
          emboss_fill="#1A1A1A", speck_area=0.0, drop_bg=False):
    """Turn trace text S into {kind: svg text} for design, emboss (and disc, coaster unless
    base is none), plus a summary dict. Writes nothing."""
    base, base_color, emboss_fill = check_options(base, base_color, emboss_fill)
    W, H = size
    if W is None:
        m = (re.search(r'width="(\d+)"\s+height="(\d+)"', s)
             or re.search(r'viewBox="0 0 (\d+) (\d+)"', s))
        W, H = (int(m.group(1)), int(m.group(2))) if m else (1000, 1000)

    paths = re.findall(r'<path[^>]*?/>', s)
    parsed = [p for p in (_parse(x) for x in paths) if p]

    # (1) optional: background = fill of the near-full-canvas path (opaque traces only)
    bg = None
    for d, fill, tx, ty in (parsed if drop_bg else []):
        bb = _bbox(d, tx, ty)
        if bb and (bb[2] - bb[0]) * (bb[3] - bb[1]) >= 0.9 * W * H:
            bg = fill
            break

    # (2) drop bg + edge specks, snap to palette, BAKE translate, group by color
    speck_limit = min(speck_area, SPECK_FRACTION * W * H)
    groups, leftovers, dropped = {}, [], {"bg": 0, "speck": 0, "empty": 0}
    for d, fill, tx, ty in parsed:
        if bg and fill == bg:
            dropped["bg"] += 1
            continue
        bb = _bbox(d, tx, ty)
        if bb:
            area = (bb[2] - bb[0]) * (bb[3] - bb[1])
            edge = bb[0] < 3 or bb[1] < 3 or bb[2] > W - 3 or bb[3] > H - 3
            if area < speck_limit and edge:
                dropped["speck"] += 1
                continue
        snapped = _hex(min(palette, key=lambda c: sum((x - y) ** 2 for x, y in zip(c, _rgb(fill)))))
        baked = _bake(d, tx, ty)
        if baked is None:
            leftovers.append((snapped, d, tx, ty))   # keep un-merged, rare
            continue
        baked = _drop_flat_subpaths(baked)
        if not baked:
            dropped["empty"] += 1
            continue
        groups.setdefault(snapped, []).append(baked)

    if not groups and not leftovers:
        raise ValueError("no shapes left to print: everything traced was background or an edge "
                         "speck (regions narrower than 2 pixels do not trace)")

    head = (f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
            f'version="1.1" width="{W}" height="{H}" viewBox="0 0 {W} {H}">\n')

    def leftover_els(fill_for):
        return [f'  <path fill="{fill_for(color)}" transform="translate({tx} {ty})" d="{d}"/>'
                for color, d, tx, ty in leftovers]

    # design: one consolidated <path> per color (+ any leftovers)
    design = "\n".join(
        [f'  <path fill="{color}" fill-rule="nonzero" d="{" ".join(ds)}"/>'
         for color, ds in groups.items()] + leftover_els(lambda c: c))
    # emboss: every color in ONE path, so the slicer makes one part
    all_ds = [d for ds in groups.values() for d in ds]
    emboss = "\n".join(
        ([f'  <path fill="{emboss_fill}" fill-rule="nonzero" d="{" ".join(all_ds)}"/>']
         if all_ds else []) + leftover_els(lambda c: emboss_fill))

    def base_el():
        if base == "circle":
            return f'  <circle cx="{W / 2}" cy="{H / 2}" r="{min(W, H) / 2}" fill="{base_color}"/>'
        return (f'  <rect x="0" y="0" width="{W}" height="{H}" rx="{min(W, H) * 0.05}" '
                f'fill="{base_color}"/>')

    files = {"design": head + design + "\n</svg>\n", "emboss": head + emboss + "\n</svg>\n"}
    if base != "none":
        files["disc"] = head + base_el() + "\n</svg>\n"
        files["coaster"] = head + base_el() + "\n" + design + "\n</svg>\n"
    return files, {"bg": bg, "dropped": dropped, "colors": len(groups),
                   "paths": sum(len(v) for v in groups.values()), "unmerged": len(leftovers),
                   "parts": len(groups) + len(leftovers)}


def finalize(svg, palette_json, out_prefix, **options):
    """Read the trace SVG and palette.json, write <out_prefix>-design.svg and friends (see
    build for OPTIONS). Returns the summary dict with the written paths."""
    check_options(**{k: v for k, v in options.items() if k in ("base", "base_color", "emboss_fill")})
    palette, size = load_palette(palette_json)
    try:
        text = Path(svg).read_text()
    except FileNotFoundError:
        raise FileNotFoundError(f"no such SVG: {svg}") from None
    except (OSError, UnicodeDecodeError) as e:
        raise ValueError(f"cannot read {svg}: {e}") from None
    files, summary = build(text, palette, size, **options)
    prefix = Path(out_prefix).expanduser().absolute()
    names = {f"{prefix.name}-{kind}.svg": doc for kind, doc in files.items()}
    with fsio.OutDir(prefix.parent, explicit=True) as od:
        od.replace_all(names, protect=fsio.identities(svg, palette_json))
    summary["written"] = [str(prefix.parent / n) for n in names]
    return summary


def add_arguments(ap):
    ap.add_argument("svg", help="traced SVG (e.g. raw.svg from marquetry trace)")
    ap.add_argument("palette", help="palette.json from marquetry posterize")
    ap.add_argument("--out-prefix", required=True, help="output path prefix, e.g. out/logo")
    ap.add_argument("--base", choices=BASES, default="circle")
    ap.add_argument("--base-color", default="#FFFFFF")
    ap.add_argument("--emboss-fill", default="#1A1A1A")
    ap.add_argument("--speck-area", type=float, default=0.0,
                    help="drop paths smaller than this (px^2, capped at 0.2%% of the canvas) "
                         "that touch the image edge; off by default, for traces of an opaque "
                         "image (posterize already removes specks)")
    ap.add_argument("--drop-bg", action="store_true",
                    help="drop the color of a path covering the whole canvas; only for traces of "
                         "an opaque image, not after marquetry posterize")


def run(a):
    r = finalize(a.svg, a.palette, a.out_prefix, base=a.base, base_color=a.base_color,
                 emboss_fill=a.emboss_fill, speck_area=a.speck_area, drop_bg=a.drop_bg)
    print(f"bg={r['bg']} dropped={r['dropped']} colors={r['colors']} "
          f"(paths {r['paths']}->{r['colors']} consolidated"
          f"{', +' + str(r['unmerged']) + ' unmerged' if r['unmerged'] else ''})")
    print("wrote " + ", ".join(r["written"]))
