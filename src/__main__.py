"""`python -m src <command>` — the command line for the whole tool.

The argument tree lives here; each command's module is imported only when that
command runs, so `sources --list` does not pay for loading the agent SDK.
"""
from __future__ import annotations

import argparse
import logging
import sys

from .nowindow import apply as _no_window

# Before anything pulls in claude-agent-sdk: its CLI children must not each open
# a console window (see src/nowindow.py).
_no_window()

from . import cli   # noqa: E402  (must follow the patch above)


def _add_scan(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-d", "--days", type=int, default=None,
                        help="freshness window in days (default: resources.yaml meta)")
    parser.add_argument("-c", "--categories", nargs="+", metavar="CAT",
                        help="categories to scan (default: the profile's focus_categories)")
    parser.add_argument("-l", "--limit-sources", type=int, default=None, metavar="N",
                        help="scan only the first N sources - a cheap smoke test")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="digest only: no progress or summary on stderr")


def _add_onboard(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("resume", help="path to a PDF/text CV, or - to read stdin")
    parser.add_argument("-y", "--yes", action="store_true",
                        help="save without asking for confirmation")


def _add_profile(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--set", nargs=2, metavar=("FIELD", "VALUE"),
                        help="set a scalar field, e.g. --set min_salary 90000")
    parser.add_argument("--add", nargs=2, metavar=("FIELD", "VALUE"),
                        help="add to a list field, e.g. --add must_have_skill Rust")
    parser.add_argument("--remove", nargs=2, metavar=("FIELD", "VALUE"),
                        help="remove from a list field")
    parser.add_argument("--reset", action="store_true", help="drop all profile overrides")
    parser.add_argument("--fields", action="store_true", help="list the editable fields")


def _add_sources(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--list", action="store_true", help="every board, not just the counts")
    parser.add_argument("--add", metavar="URL", help="add a board; its render is auto-detected")
    parser.add_argument("--category", default="job", help="category for --add (default: job)")
    parser.add_argument("--name", help="display name for --add (default: derived from the URL)")
    parser.add_argument("--remove", metavar="NAME", help="remove a board you added")
    parser.add_argument("--on", metavar="NAME", help="enable a board")
    parser.add_argument("--off", metavar="NAME", help="disable a board")


def _add_config(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"),
                        help="threshold | top_n | window | cap")
    parser.add_argument("--reset", action="store_true",
                        help="drop every override, back to resources.yaml")


def _add_export(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-s", "--min-score", type=int, default=0,
                        help="skip positions below this score (default: keep all)")
    parser.add_argument("-d", "--days", type=int, default=0,
                        help="only scans from the last N days (default: all history)")


# command → (help text, argument builder, "module:function" run target)
_COMMANDS = {
    "scan": ("scan the boards and print a ranked digest", _add_scan, "scan:run"),
    "onboard": ("build the active profile from a résumé", _add_onboard, "onboard:run"),
    "profile": ("show or edit the active profile", _add_profile, "commands:run_profile"),
    "sources": ("list, add, remove or toggle boards", _add_sources, "commands:run_sources"),
    "config": ("show or change the matching settings", _add_config, "commands:run_config"),
    "export": ("export scored positions to CSV", _add_export, "export:run"),
}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src",
        description="Scan job boards and rank what fits your profile.",
    )
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="INFO logging and the full traceback on failure")
    subcommands = parser.add_subparsers(dest="command", required=True)

    for name, (help_text, add_arguments, _target) in _COMMANDS.items():
        subcommand = subcommands.add_parser(name, help=help_text, description=help_text)
        add_arguments(subcommand)

    return parser


def _resolve(target: str):
    """'module:function' → the callable, imported on demand."""
    module_name, function_name = target.split(":")
    module = __import__(f"{__package__}.{module_name}", fromlist=[function_name])

    return getattr(module, function_name)


def main(argv: list[str] | None = None) -> int:
    cli.use_utf8()                      # --help exits inside parse_args: do this first

    args = _build_parser().parse_args(argv)

    cli.quiet_shutdown_noise()
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        stream=sys.stderr, format="%(levelname)s %(message)s")

    try:
        return _resolve(_COMMANDS[args.command][2])(args)
    except KeyboardInterrupt:           # Ctrl+C is not a crash
        cli.note("interrupted")

        return 130


if __name__ == "__main__":
    raise SystemExit(main())
