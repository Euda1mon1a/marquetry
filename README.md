<p align="center"><img src="https://raw.githubusercontent.com/Euda1mon1a/marquetry/main/docs/icon/icon-256.png" width="128" alt="Marquetry icon"></p>

# Marquetry

Marquetry turns a flat logo or sticker image into SVGs you can 3D print in several filament
colors. Each color becomes one path, so the slicer imports one part per filament instead of
dozens of fragments. It also writes a base disc and a single-color version for embossing.

It is built for coasters, signs, keychains and magnets on a multi-color printer (developed on a
Bambu Lab H2C). The name comes from the woodworking craft of inlaying flat colored pieces to form
a picture.

## Install

```
uv tool install marquetry-svg
```

or `pipx install marquetry-svg`. The package is `marquetry-svg` because `marquetry` on PyPI is
an unrelated project; the command it installs is `marquetry`.

Requirements: Python 3.12 or newer. Everything else (Pillow, NumPy, SciPy, scikit-learn,
vtracer, PyQt6) installs from PyPI wheels; there is nothing to install with Homebrew. Tested on
macOS on Apple silicon.

## Use it

**App.** Run `marquetry` with no arguments. Drop images on the window (or use Browse), pick the
number of colors and a base shape (circle, square or none), and press Make Coasters. Each
image's SVGs land in a folder next to it, the row shows how many parts the design has, and
Reveal opens that folder.

**Command line.**

```
marquetry coaster logo.png --palette F6B820,0D5E1E,AD6D1A --base circle --preview
```

Outputs, in `logo.png-coaster/` next to the image unless you pass `--out`. The whole file name
is part of the folder name, so `logo.png`, `logo.jpg` and `.logo.png` never share a folder:

| File | What it is |
|---|---|
| `logo-design.svg` | One path per color on a transparent background, no base. |
| `logo-coaster.svg` | Base disc plus design, for a single import. |
| `logo-disc.svg` | Base disc only. |
| `logo-emboss.svg` | The whole design as one single-color path, for a one-filament raise or deboss. |
| `preview-*.png` | With `--preview`: the coaster on grey, the design on magenta. |
| `clean-on-white.png`, `palette.json`, `raw.svg` | The posterized image, the palette and the raw trace, for checking. |

Running it again replaces these files and removes any that the new settings no longer produce
(for example the disc after `--base none`). Other files in the folder are left alone. Nothing
is written until every step has succeeded, and the files are replaced together: if anything is
refused (say, one of the names is now a symlink), the earlier outputs stay as they were. The
default folder must not be a symlink; a folder you pass with `--out` may be one.

Other options: `--colors N` (automatic palette, 1 to 32), `--base circle|square|none`,
`--base-color`, `--speckle N` (see below), and `--val-bg`/`--sat-bg` (what counts as
background). `marquetry coaster --help` lists them all.

**Pick the palette yourself.** The automatic palette (`--colors N`, which the app uses) keeps
every color it finds. On an anti-aliased or JPEG image that can include a thin extra part: the
halo where two colors blend. Give that part the same filament as its neighbor in the slicer, or
rerun with `--fold-halos` or an explicit `--palette`. To pick colors, run
`marquetry posterize logo.png work --colors 5` once; it prints each cluster's share of the ink.
Then pass the colors you want with `--palette`, which is used exactly as given. Check
`preview-design.png`: it is drawn on magenta so you can see that small counters (the holes in A,
o and e) stayed open.

Two cleanups for the automatic palette are off by default, because both can remove a real
color, and each logs every color it removes (for example `folded #F6B820, 3,044 px, into
#F58230 (3,044 px)`):

- `--fold-halos` folds a color into its neighbors when it is almost all thin edges (2 px wide or
  less) and its color is a blend or shade of what it touches. A thin outline, pinstripe or line
  of text in a color close to its surroundings can be folded too.
- `--merge-close RGB` merges colors closer than that RGB distance, keeping the bigger one. 24
  cleans up JPEG noise that splits one color, but it also merges two real colors that close.

**Background.** Transparent pixels are background. So are light, unsaturated pixels (white to
light grey), which suits a logo saved on white. Background becomes holes in the design, and the
base shows through them. To keep white as a printed color instead, pass `--val-bg 1`; then only
transparent pixels count as background.

**Specks.** `--speckle N` (default 4) gives any patch of ink smaller than N x N pixels the color
around it, which cleans up JPEG noise. Holes are never filled. For very small images, where a
few pixels are real detail, pass `--speckle 1`.

**Step by step**, when you want to inspect or swap a stage:

```
marquetry posterize logo.png work --palette F6B820,0D5E1E,AD6D1A
marquetry trace work/clean-color.png work/raw.svg
marquetry finalize work/raw.svg work/palette.json --out-prefix work/logo --base circle
```

`trace` expects a posterized image (at most 32 colors). `finalize` is built for that trace:
paths in absolute M, L, C, Q and Z commands, optionally positioned with `translate()`, filled
with a hex or `rgb()` color. A path with other commands is kept as its own element instead of
being merged, and a path without a hex or `rgb()` fill is ignored. Two cleanups are off by
default and meant only for a trace of an opaque image: `--drop-bg` removes the color of a path
that covers the whole canvas, and `--speck-area N` drops small paths touching the edge. Don't
use them after `posterize`, where the background is already transparent and specks are already
gone; there they would only remove real ink, such as a badge cropped to its edge.

## Print it fast

Color that runs the full height of a print makes every layer a filament-swap layer. Keep the
color in the top few layers instead:

1. Import `-disc.svg` and extrude it about 3 mm in one filament.
2. Import `-design.svg` on top at about 0.8 mm (four layers at 0.2 mm) and assign one filament
   to each part.

Only those top layers swap filament. On one coaster this cut the print from about 7 hours to
about 1 hour. That is why `-design.svg` ships with no base. For a smooth face, print it design
side down.

The SVGs are sized in the source image's pixels; scale them in the slicer to the size you want.

## How it works

Three steps: posterize, trace with [vtracer](https://github.com/visioncortex/vtracer), then
finalize. The choices that matter:

- **Quantize before tracing.** Tracing a JPEG directly turns JPEG noise and anti-aliased edges
  into hundreds of near-duplicate shades, each its own path (878 paths on one test logo,
  against 3 after posterizing).
- **No median despeckle.** A median filter of 3 or more closes thin letter counters. The speck
  filter works on connected ink regions instead, so it can't close a hole.
- **One color at a time.** Each color is traced from its own layer, with everything else
  transparent, so a color printed on top of another is a hole in the one below rather than a
  part lying over it. Tracing all colors in one pass would let vtracer merge small details (an
  eye, the dot of an i) into the color around them. Because each side of a shared edge is
  traced separately, the outlines don't match exactly; see Limits.
- **One path per color.** vtracer writes each shape as relative coordinates positioned by
  `transform="translate(...)"`. Marquetry adds each translate into the coordinates before
  merging the shapes, because one path element can't carry a transform per shape.
  `fill-rule="nonzero"` keeps the holes.

## Limits

- Made for flat artwork with a few solid colors: logos, stickers, icons. Photos and gradients
  posterize into blotches.
- As a bound on run time, an image that breaks into more than 250,000 separate regions per
  megapixel (at least 50,000) is refused before tracing. Busy artwork stays well under it (8 px
  cross-hatching is about 21,000 per megapixel, 6 px about 37,000). `--max-regions N` raises the
  limit; `--max-regions 0` removes it.
- Outlines follow the posterized image to within about 1.5 px (up to 2 px on small text).
  Where two colors share a curved or diagonal edge, their parts can overlap by up to about
  1.5 px, and a gap of about a pixel can open where colors meet at a corner. For scale: on a
  90 mm coaster made from a 400 px image, a pixel is about 0.2 mm.
- Holes and details narrower than 2 pixels are lost in tracing. Scale a tiny image up (nearest
  neighbor) before converting it.
- Only tested on macOS. vtracer and PyQt6 publish wheels for Linux and Windows, but nobody has
  run Marquetry there.

## Related

- [ObjectHealer](https://github.com/Euda1mon1a/object-healer): drag-and-drop mesh repair for 3D
  printing.
- [Splinter](https://github.com/Euda1mon1a/splinter): splits a single 3D mesh into printable
  parts.

## License

GPL-3.0-or-later, because the app uses PyQt6, which is GPL. See
[LICENSE](https://github.com/Euda1mon1a/marquetry/blob/main/LICENSE).

Provided as-is; bug reports are welcome, fixes are not guaranteed.
