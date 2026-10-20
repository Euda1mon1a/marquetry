"""One shot: flat logo image -> print-ready coaster SVGs in seconds.

Runs the whole pipeline: posterize -> vtracer trace -> finalize and consolidate.

Outputs in OUT (default <image file name>-coaster/ next to the image, e.g. logo.png-coaster/):
    <stem>-design.svg   N colors merged to ONE path each, transparent, NO base.
                        Emboss this ~0.8 mm on top of a thick base so only the top
                        few layers swap color, which makes a fast multi-color slice.
    <stem>-coaster.svg  base disc + design in one file (single import).
    <stem>-disc.svg     base disc only.
    <stem>-emboss.svg   single-color silhouette as one path (one-filament raise or deboss).
    clean-on-white.png  posterize sanity check;  palette.json;  raw.svg (debug).

PREFER --palette (explicit hex). Auto-KMeans (--colors) can merge a real ink color into a
numerous anti-alias halo. Run once with --colors 5 to see the clusters.
"""
from pathlib import Path

from . import finalize, fsio, posterize, preview, trace

BAMBU_HINT = ("Slicer: import -disc.svg and extrude ~3 mm (1 filament); import -design.svg at "
              "~0.8 mm on top; assign filaments. Only the top ~4 layers swap color.")
SHARED_OUTPUTS = ("clean-color.png", "clean-emboss.png", "clean-on-white.png", "palette.json",
                  "raw.svg", "preview-coaster.png", "preview-design.png")
KINDS = ("design", "coaster", "disc", "emboss")


def out_dir_for(image):
    """<image file name>-coaster next to the image. The whole name is kept, extension and any
    leading dot included, so logo.png and logo.jpg, 'my logo' and 'my_logo', or .logo.png and
    logo.png never share a folder."""
    p = Path(image)
    return p.parent / f"{p.name}-coaster"


def file_stem(image):
    """Output file stem: the name before the last dot, leading dots stripped so the outputs
    aren't hidden, computed the same way on every Python version."""
    name = Path(image).name.lstrip(".") or "image"
    return name.rsplit(".", 1)[0] if "." in name[1:] else name


def output_names(stem):
    return [f"{stem}-{k}.svg" for k in KINDS] + list(SHARED_OUTPUTS)


def make_coaster(image, *, out=None, palette=None, colors=4, base="circle",
                 base_color="#FFFFFF", speckle=4, val_bg=0.80, sat_bg=0.20,
                 fold_halos=False, merge_close=0.0, max_regions=None,
                 make_preview=False, log=print):
    # validate everything before touching the disk
    posterize.check_settings(colors=colors if not palette else 1, speckle=speckle)
    if base not in finalize.BASES:
        raise ValueError(f"base must be one of {finalize.BASES}")
    base_color = posterize.parse_hex(base_color, "--base-color")
    img = Path(image).expanduser().absolute()
    rgba = posterize.load_image(img)
    stem = file_stem(img)
    explicit = bool(out)
    out = Path(out).expanduser().absolute() if out else out_dir_for(img)

    # Everything is computed in memory first, so a failure at any step writes nothing.
    # 1. posterize (median off keeps thin letter counters; speck filter on ink only)
    pal, cls = posterize.quantize(rgba, colors=colors, palette=palette, val_bg=val_bg,
                                  sat_bg=sat_bg, median=1, speckle=speckle,
                                  fold_halos=fold_halos, merge_close=merge_close,
                                  max_regions=max_regions, log=log)
    H, W = cls.shape

    # 2. trace one color at a time, so the colors are cut out of each other
    raw, _ = trace.trace_array(posterize.color_image(pal, cls), max_regions=0)   # checked above

    # 3. finalize: snap, BAKE transforms, consolidate one path per color, + base. Posterize
    # already made the background transparent and filtered specks by region, so finalize's
    # geometric guesses (full-canvas background, small paths touching the edge) stay off:
    # here they would only ever remove real ink, like the corners of a tight crop.
    docs, f = finalize.build(raw, pal, (W, H), base=base, base_color=base_color,
                             drop_bg=False, speck_area=0)

    previews, skipped = {}, None
    if make_preview:
        # coaster preview on neutral gray so a white base disc is visible; design on magenta
        # so transparent counters stand out
        jobs = ([("coaster", "#b8b8b8")] if base != "none" else []) + [("design", "#ff00ff")]
        try:
            for kind, bgc in jobs:
                previews[f"preview-{kind}.png"] = preview.render(docs[kind], 500, 500,
                                                                 background=bgc)
        except preview.PreviewUnavailable as e:
            previews, skipped = {}, e

    # Write through one opened folder (see fsio): every name is checked first (including
    # that none is the input image, by inode, so case-insensitive names can't slip past),
    # then the new files replace the old ones, then outputs this run doesn't produce go.
    pfiles, r = posterize.outputs(pal, cls)
    files = dict(pfiles)
    files["raw.svg"] = raw
    files.update({f"{stem}-{kind}.svg": doc for kind, doc in docs.items()})
    files.update(previews)
    with fsio.OutDir(out, explicit=explicit) as od:
        od.replace_all(files, stale=output_names(stem), protect=fsio.identities(img))
        out = od.path

    log(f"[1/3] posterize: {W}x{H}  ink {100 * r['ink']:.1f}%  colors={len(r['palette'])}"
        f"{' (explicit)' if palette else ' (auto KMeans)'}")
    log("[2/3] traced (vtracer, one layer per color)")
    log(f"[3/3] finalize: dropped={f['dropped']} parts={f['parts']} "
        f"(paths {f['paths']}->{f['colors']} consolidated)")
    if skipped:
        log(f"previews skipped: {skipped}")
    elif previews:
        log("previews: " + ", ".join(previews))
    return out, stem, f


def add_arguments(ap):
    ap.add_argument("image")
    ap.add_argument("--out", default="",
                    help="output folder (default: <image file name>-coaster/ next to the image)")
    ap.add_argument("--palette", default="", help="explicit hex colors, e.g. F6B820,0D5E1E,AD6D1A")
    ap.add_argument("--colors", type=int, default=4,
                    help="ink colors for auto KMeans when no --palette (1-32)")
    ap.add_argument("--base", choices=finalize.BASES, default="circle")
    ap.add_argument("--base-color", default="#FFFFFF", help="hex color of the base disc")
    ap.add_argument("--speckle", type=int, default=4,
                    help="ink specks smaller than N x N pixels take the surrounding color "
                         "(0 or 1 keeps everything)")
    ap.add_argument("--val-bg", type=float, default=0.80,
                    help="light pixels count as background when brightness > this (0-1); "
                         "pass 1 to keep white as a color")
    ap.add_argument("--sat-bg", type=float, default=0.20, help="...and saturation < this (0-1)")
    posterize.add_palette_options(ap)
    ap.add_argument("--preview", action="store_true", help="also write preview PNGs")


def run(a):
    palette = posterize.parse_palette(a.palette) if a.palette.strip() else None
    out, stem, _ = make_coaster(a.image, out=a.out or None, palette=palette, colors=a.colors,
                                base=a.base, base_color=a.base_color, speckle=a.speckle,
                                val_bg=a.val_bg, sat_bg=a.sat_bg, fold_halos=a.fold_halos,
                                merge_close=a.merge_close, max_regions=a.max_regions,
                                make_preview=a.preview)
    print(f"\nDONE -> {out}")
    for kind in KINDS:
        p = out / f"{stem}-{kind}.svg"
        if p.exists():
            print(f"  {p.name}")
    print("\n" + BAMBU_HINT)
