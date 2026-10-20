"""`marquetry` command: no arguments opens the drag-and-drop app; subcommands run the pipeline."""
import argparse
import sys

from . import __version__, coaster, finalize, posterize, trace

COMMANDS = {
    "coaster": (coaster, "image -> print-ready SVGs in one step (posterize, trace, finalize)"),
    "posterize": (posterize, "step 1: quantize an image to a small palette"),
    "trace": (trace, "step 2: trace the posterized PNG with vtracer"),
    "finalize": (finalize, "step 3: merge one path per color and add the base"),
}


def build_parser():
    ap = argparse.ArgumentParser(
        prog="marquetry",
        description="Flat logo image -> filament-ready multi-color SVGs for 3D printing. "
                    "Run with no arguments to open the drag-and-drop app.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("gui", help="open the drag-and-drop app (same as no arguments)")
    for name, (mod, help_text) in COMMANDS.items():
        p = sub.add_parser(name, help=help_text, description=help_text)
        mod.add_arguments(p)
    return ap


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    a = build_parser().parse_args(argv)
    if a.command in (None, "gui"):
        from .gui import main as gui_main
        return gui_main()
    try:
        COMMANDS[a.command][0].run(a)
    except (OSError, ValueError, OverflowError, MemoryError) as e:
        # missing or unreadable files, bad settings or palette.json, vtracer failures
        print(f"marquetry: {e}", file=sys.stderr)
        return 2
    return 0
