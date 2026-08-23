"""Best-effort progress reporting for a long scan (a caller-supplied callback +
a periodic heartbeat so the owner knows a multi-minute scan isn't dead)."""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger("job-searcher")

ProgressCB = Callable[[str], Awaitable[None]]
HEARTBEAT_SECONDS = 60  # "still alive" ticker cadence during a long /search run


async def _emit(on_progress: ProgressCB | None, text: str) -> None:
    """A broken/absent callback must never break the scan, so errors are swallowed."""
    if on_progress is None:
        return

    try:
        await on_progress(text)
    except Exception:
        log.warning("on_progress callback failed", exc_info=True)


def _elapsed(started: float) -> str:
    secs = int(time.monotonic() - started)
    return f"{secs // 60}m{secs % 60:02d}s"


async def _heartbeat(on_progress: ProgressCB | None, progress: dict, total: int) -> None:
    """Ticks every HEARTBEAT_SECONDS; covers fetch+extract (`progress`) and
    matching (`progress["matching"]`, set once scoring starts). Every tick
    carries elapsed time and, while scoring, the batch counter — an identical
    line repeated 8 times reads as a hang even when the run is healthy.
    Cancelled by run_search's finally block; a bad tick never kills the loop."""
    started = time.monotonic()

    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)

        try:
            if progress.get("matching") is not None:
                batch = progress.get("batch")

                if batch:
                    await _emit(on_progress,
                                f"Scoring · batch {batch[0]}/{batch[1]} · {_elapsed(started)}")
                else:
                    await _emit(on_progress,
                                f"Scoring {progress['matching']} · {_elapsed(started)}")
            else:
                await _emit(on_progress,
                            f"{progress['sources_done']}/{total} sources · "
                            f"{progress['positions']} found · {_elapsed(started)}")
        except Exception:
            log.warning("heartbeat tick failed", exc_info=True)
