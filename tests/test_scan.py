"""The `scan` command: argument surface and the run path. No network and no
LLM — run_search is faked."""
from __future__ import annotations

from src import scan
from src.__main__ import _build_parser, main


def _fake_run_search(recorder: dict, digest: str = "DIGEST", stats: dict | None = None):
    """Records the kwargs the command passes down, returns a canned result."""
    async def fake(**kwargs):
        recorder.update(kwargs)

        return digest, stats if stats is not None else {"sources": 2, "scored": 1}

    return fake


def _profile(monkeypatch, **over):
    base = {"identity": {"role": "Backend Engineer"}, "focus_categories": ["job"]}
    base.update(over)
    monkeypatch.setattr(scan.config, "load_profile", lambda: base)


# ── argument surface ───────────────────────────────────────────────────────
def test_defaults_leave_every_choice_to_config():
    args = _build_parser().parse_args(["scan"])

    assert args.days is None and args.categories is None
    assert args.limit_sources is None and args.quiet is False


def test_every_flag_parses():
    args = _build_parser().parse_args(
        ["-v", "scan", "-d", "7", "-c", "job", "bounty", "-l", "3", "-q"])

    assert args.days == 7
    assert args.categories == ["job", "bounty"]
    assert args.limit_sources == 3
    assert args.quiet is True and args.verbose is True


# ── run path ───────────────────────────────────────────────────────────────
def test_digest_on_stdout_summary_on_stderr(monkeypatch, capsys):
    calls: dict = {}
    monkeypatch.setattr(scan, "run_search", _fake_run_search(calls))
    _profile(monkeypatch)

    rc = main(["scan", "--days", "3"])
    out = capsys.readouterr()

    assert rc == 0
    assert out.out.strip() == "DIGEST"          # only the digest is redirectable
    assert "sources=2 scored=1" in out.err      # progress/summary stay on stderr
    assert calls["days"] == 3 and calls["categories"] == ["job"]
    assert calls["limit_sources"] is None and calls["on_progress"] is not None


def test_explicit_categories_win_over_the_profile(monkeypatch, capsys):
    calls: dict = {}
    monkeypatch.setattr(scan, "run_search", _fake_run_search(calls))
    _profile(monkeypatch)

    main(["scan", "-c", "bounty", "grant", "-l", "2"])
    capsys.readouterr()

    assert calls["categories"] == ["bounty", "grant"]
    assert calls["limit_sources"] == 2


def test_profileless_run_falls_back_to_default_categories(monkeypatch, capsys):
    calls: dict = {}
    monkeypatch.setattr(scan, "run_search", _fake_run_search(calls))
    monkeypatch.setattr(scan.config, "load_profile", dict)   # empty profile

    main(["scan"])
    capsys.readouterr()

    assert calls["categories"] == ["job", "vc_board"]


def test_quiet_prints_the_digest_and_nothing_else(monkeypatch, capsys):
    calls: dict = {}
    monkeypatch.setattr(scan, "run_search", _fake_run_search(calls))
    _profile(monkeypatch)

    rc = main(["scan", "--quiet"])
    out = capsys.readouterr()

    assert rc == 0
    assert out.out.strip() == "DIGEST"
    assert out.err == ""
    assert calls["on_progress"] is None         # no heartbeat noise either


def test_failed_scan_reports_and_exits_1_without_traceback(monkeypatch, capsys):
    async def boom(**kwargs):
        raise RuntimeError("board on fire")

    monkeypatch.setattr(scan, "run_search", boom)
    _profile(monkeypatch)

    rc = main(["scan"])
    out = capsys.readouterr()

    assert rc == 1
    assert out.out == ""
    assert "board on fire" in out.err
    assert "-v for the traceback" in out.err   # the trace is opt-in, not the default


def test_ctrl_c_exits_130_not_a_traceback(monkeypatch, capsys):
    async def interrupted(**kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(scan, "run_search", interrupted)
    _profile(monkeypatch)

    rc = main(["scan"])

    assert rc == 130
    assert "interrupted" in capsys.readouterr().err


def test_summary_skips_stats_the_run_never_set():
    assert scan._summary({"sources": 3, "scored": 0}) == "sources=3 scored=0"
    assert scan._summary({}) == ""
