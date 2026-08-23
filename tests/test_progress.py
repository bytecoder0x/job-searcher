"""Characterization tests for src/progress.py — pin CURRENT behavior."""
from __future__ import annotations

import asyncio

from src import progress


# ── _emit ─────────────────────────────────────────────────────────────────
def test_emit_none_callback_noop():
    asyncio.run(progress._emit(None, "text"))  # must not raise


def test_emit_raising_callback_swallowed():
    async def bad_cb(text):
        raise ValueError("boom")

    asyncio.run(progress._emit(bad_cb, "text"))  # must not raise/propagate
