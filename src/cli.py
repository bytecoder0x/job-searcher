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


# Where the shutdown noise comes from: asyncio finalizes the transports of the
# SDK's child processes after the event loop is already gone, and the garbage
# collector reports whatever that raises. Seen in the wild as ValueError("I/O
# operation on closed pipe") and RuntimeError("Event loop is closed") — same
# cause, different message, so the filter keys on the finalizer, not the text.
_SHUTDOWN_FINALIZER = "BaseSubprocessTransport.__del__"


def quiet_shutdown_noise() -> None:
    """Hide the traceback asyncio prints from that finalizer once the command
    has already succeeded. Anything raised anywhere else still surfaces."""
    fallback = sys.unraisablehook

    def hook(unraisable) -> None:
        source = getattr(unraisable.object, "__qualname__", "") or ""

        if _SHUTDOWN_FINALIZER in source:
            return

        fallback(unraisable)

    sys.unraisablehook = hook
