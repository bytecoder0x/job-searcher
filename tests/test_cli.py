"""src/cli.py — console plumbing shared by the command-line entry points."""
from __future__ import annotations

import sys
from asyncio.base_subprocess import BaseSubprocessTransport

from src import cli


# The real finalizer the noise comes from — using it, rather than a stand-in,
# is what makes this test fail if asyncio ever renames or moves it.
_FINALIZER = BaseSubprocessTransport.__del__


class _Unraisable:
    def __init__(self, exc, obj=None):
        self.exc_value = exc
        self.object = obj


def test_shutdown_noise_from_the_transport_finalizer_is_hidden(monkeypatch):
    """Both messages seen in the wild come from the same finalizer after a
    command already succeeded, so the filter keys on the source, not the text."""
    seen = []
    monkeypatch.setattr(sys, "unraisablehook", seen.append)
    cli.quiet_shutdown_noise()

    sys.unraisablehook(_Unraisable(ValueError("I/O operation on closed pipe"), _FINALIZER))
    sys.unraisablehook(_Unraisable(RuntimeError("Event loop is closed"), _FINALIZER))

    assert seen == []


def test_a_real_unraisable_still_surfaces(monkeypatch):
    """Same exception types from anywhere else must not be swallowed."""
    seen = []
    monkeypatch.setattr(sys, "unraisablehook", seen.append)
    cli.quiet_shutdown_noise()

    bug = _Unraisable(RuntimeError("Event loop is closed"), object())
    elsewhere = _Unraisable(ValueError("a real bug worth seeing"))
    sys.unraisablehook(bug)
    sys.unraisablehook(elsewhere)

    assert seen == [bug, elsewhere]


def test_note_writes_to_stderr_only(capsys):
    cli.note("progress")
    out = capsys.readouterr()

    assert out.err.strip() == "progress"
    assert out.out == ""


def test_use_utf8_survives_a_stream_without_reconfigure(monkeypatch):
    monkeypatch.setattr(sys, "stdout", object())
    monkeypatch.setattr(sys, "stderr", object())

    cli.use_utf8()          # must not raise
