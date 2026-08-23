"""src/cli.py — console plumbing shared by the command-line entry points."""
from __future__ import annotations

import sys

from src import cli


def test_shutdown_noise_hook_swallows_only_the_closed_pipe_error(monkeypatch):
    """Windows finalizes the SDK's subprocess transports after the loop is gone;
    that one traceback is noise. A real unraisable must still surface."""
    seen = []
    monkeypatch.setattr(sys, "unraisablehook", seen.append)
    cli.quiet_shutdown_noise()

    class _Unraisable:
        def __init__(self, exc):
            self.exc_value = exc

    sys.unraisablehook(_Unraisable(ValueError("I/O operation on closed pipe")))

    assert seen == []

    real = _Unraisable(ValueError("a real bug worth seeing"))
    sys.unraisablehook(real)

    assert seen == [real]


def test_note_writes_to_stderr_only(capsys):
    cli.note("progress")
    out = capsys.readouterr()

    assert out.err.strip() == "progress"
    assert out.out == ""


def test_use_utf8_survives_a_stream_without_reconfigure(monkeypatch):
    monkeypatch.setattr(sys, "stdout", object())
    monkeypatch.setattr(sys, "stderr", object())

    cli.use_utf8()          # must not raise
