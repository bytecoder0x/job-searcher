"""Console plumbing shared by the command-line entry points (scan, onboard,
export): UTF-8 output, stderr notes, and Windows asyncio shutdown noise."""
from __future__ import annotations

import sys


def use_utf8() -> None:
    """Windows consoles default to a legacy codepage and the output carries
    emoji. Best-effort: a stream without reconfigure (pytest capture) is fine."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def note(text: str) -> None:
    """Anything that is not the result goes to stderr, so `> out.txt` is clean."""
    print(text, file=sys.stderr, flush=True)


def quiet_shutdown_noise() -> None:
    """The SDK spawns a CLI child per LLM call; on Windows their transports are
    finalized after the event loop is gone, so asyncio prints an 'I/O operation
    on closed pipe' traceback once the command already succeeded. Cosmetic and
    not fixable from here — swallow that one, pass everything else through."""
    fallback = sys.unraisablehook

    def hook(unraisable) -> None:
        exc = unraisable.exc_value

        if isinstance(exc, ValueError) and "closed pipe" in str(exc):
            return

        fallback(unraisable)

    sys.unraisablehook = hook
