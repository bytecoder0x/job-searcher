"""Spawn child processes without a console window (Windows only).

claude-agent-sdk does not call an HTTP API — it launches the Claude Code CLI as a
child process for EVERY LLM call. On Windows each child gets its own console, so
the owner sees a terminal window flash per call: one per chat message (router),
one per source (extract), plus prefilter/match batches — dozens per /search.

Injecting CREATE_NO_WINDOW into every spawn keeps those children invisible while
leaving stdout/stderr piping intact. No-op on non-Windows platforms.
"""
from __future__ import annotations

import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000

_applied = False


def _with_flag(kwargs: dict) -> dict:
    """Adds CREATE_NO_WINDOW without clobbering flags a caller already set."""
    kwargs["creationflags"] = kwargs.get("creationflags", 0) | CREATE_NO_WINDOW
    return kwargs


def apply() -> None:
    """Patches the process-spawning entry points the SDK can reach. Idempotent;
    safe to call from several entry points. Each patch is guarded separately so a
    missing/renamed dependency degrades to 'one path unpatched', never a crash."""
    global _applied

    if _applied or sys.platform != "win32":
        return

    _applied = True

    # 1. Plain subprocess (SDK version-dependent, and anything else we shell out to).
    _orig_popen = subprocess.Popen.__init__

    def _popen(self, *args, **kwargs):
        return _orig_popen(self, *args, **_with_flag(kwargs))

    subprocess.Popen.__init__ = _popen

    # 2. asyncio subprocess — the async path an async SDK client uses.
    try:
        import asyncio

        _orig_exec = asyncio.create_subprocess_exec

        async def _exec(*args, **kwargs):
            return await _orig_exec(*args, **_with_flag(kwargs))

        asyncio.create_subprocess_exec = _exec
    except Exception:
        pass

    # 3. anyio — what claude-agent-sdk actually uses under the hood.
    try:
        import anyio

        _orig_open = anyio.open_process

        async def _open(*args, **kwargs):
            return await _orig_open(*args, **_with_flag(kwargs))

        anyio.open_process = _open
    except Exception:
        pass
