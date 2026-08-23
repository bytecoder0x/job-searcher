"""Résumé → profile onboarding: extract text (PDF/plain) and turn it into a
profile dict via one LLM call. The generic entry point behind "give me your CV
and I'll configure the search" — writes data/profile.active.yaml, never the
bundled seed. Deterministic parsing/normalisation is Python (0 tokens); only the
extraction itself is an LLM call."""
from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

from . import cli, config, llm
from .llm import _parse_json_object, _skill

_MAX_RESUME_CHARS = 14000     # cap the prompt; a CV over this is truncated, with a marker so the LLM knows the tail is missing
_MIN_RESUME_CHARS = 80        # below this the extraction failed (scanned/image PDF)


def extract_resume_text(data: bytes, filename: str = "") -> str:
    """Plain text from an uploaded résumé — PDF via pypdf, otherwise decoded as
    text. Returns '' if nothing readable came out (caller reports the failure)."""
    name = (filename or "").lower()

    if name.endswith(".pdf") or data[:5] == b"%PDF-":
        try:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data))

            return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
        except Exception:
            return ""

    try:
        return data.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


async def profile_from_resume(resume_text: str) -> dict:
    """One LLM call (main model): résumé text → normalised profile dict."""
    system = _skill("resume-to-profile")
    stripped = resume_text.strip()
    text = stripped[:_MAX_RESUME_CHARS]

    if len(stripped) > _MAX_RESUME_CHARS:
        text += "\n[...truncated]"

    user = (f"Résumé:\n\n{text}\n\n"
            f"Return ONLY the profile as a single JSON object per the schema.")
    out, _ = await llm._run_agent(system, user, config.MAIN_MODEL)

    return _normalize_profile(_parse_json_object(out))


def _with_vc_board(cats: list[str]) -> list[str]:
    """Ensure a job seeker also scans vc_board sources (a major slice of the
    catalog). Empty → ['job', 'vc_board']; if 'job' is present, add 'vc_board'."""
    cats = cats or ["job"]

    if "job" in cats and "vc_board" not in cats:
        cats = cats + ["vc_board"]

    return cats


def _normalize_profile(d: dict) -> dict:
    """Coerce the LLM's object into the profile.yaml shape, filling missing keys
    with safe defaults so a partial answer can't produce a broken profile."""
    d = d if isinstance(d, dict) else {}
    ident = d.get("identity") if isinstance(d.get("identity"), dict) else {}
    skills = d.get("skills") if isinstance(d.get("skills"), dict) else {}
    prefs = d.get("preferences") if isinstance(d.get("preferences"), dict) else {}
    excl = d.get("exclude") if isinstance(d.get("exclude"), dict) else {}

    def _slist(v):
        return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []

    return {
        # vc_board (VC/ecosystem job boards) is a major slice of the catalog, so
        # a job seeker should scan it too — never silently drop it when "job" is set.
        "focus_categories": _with_vc_board(_slist(d.get("focus_categories"))),
        "identity": {
            "name": str(ident.get("name") or "").strip(),
            "role": str(ident.get("role") or "").strip(),
            "seniority": str(ident.get("seniority") or "junior to senior").strip(),
            "years_experience": ident.get("years_experience")
            if isinstance(ident.get("years_experience"), int) else None,
        },
        "skills": {
            "must_have": _slist(skills.get("must_have")),
            "nice_to_have": _slist(skills.get("nice_to_have")),
            "languages_spoken": _slist(skills.get("languages_spoken")) or ["en"],
        },
        "preferences": {
            "work_format": _slist(prefs.get("work_format")) or ["remote", "hybrid", "onsite"],
            "locations": _slist(prefs.get("locations")),
            "relocation_ok": bool(prefs.get("relocation_ok", True)),
            "employment": _slist(prefs.get("employment")) or ["full-time"],
            "min_salary_usd": prefs.get("min_salary_usd")
            if isinstance(prefs.get("min_salary_usd"), (int, float)) else None,
            "salary_target_usd": prefs.get("salary_target_usd")
            if isinstance(prefs.get("salary_target_usd"), (int, float)) else None,
            "timezones_ok": _slist(prefs.get("timezones_ok")),
        },
        "exclude": {
            "keywords": _slist(excl.get("keywords")) or ["intern", "internship", "unpaid"],
            "seniority_below": str(excl.get("seniority_below") or "junior").strip(),
        },
        "summary": str(d.get("summary") or "").strip(),
    }


def is_usable_profile(profile: dict) -> bool:
    """A profile is usable if extraction found at least a role or some skills —
    guards against saving an empty profile from an unreadable résumé."""
    ident = profile.get("identity") or {}
    skills = profile.get("skills") or {}

    return bool(str(ident.get("role") or "").strip() or (skills.get("must_have") or []))


def format_profile_preview(profile: dict) -> str:
    """Human-readable preview shown before the profile is saved (plain text)."""
    ident = profile.get("identity") or {}
    skills = profile.get("skills") or {}
    prefs = profile.get("preferences") or {}
    lines = ["🧭 Profile I built from your résumé:", ""]

    if ident.get("name"):
        lines.append(f"👤 {ident['name']}")

    lines.append(f"💼 {ident.get('role') or '—'}  ·  {ident.get('seniority') or '—'}")
    must = ", ".join((skills.get("must_have") or [])[:12])
    nice = ", ".join((skills.get("nice_to_have") or [])[:12])
    lines.append(f"🛠 must-have: {must or '—'}")

    if nice:
        lines.append(f"➕ nice-to-have: {nice}")

    wf = ", ".join(prefs.get("work_format") or [])
    lines.append(f"📍 work: {wf or '—'}  ·  relocation: {'yes' if prefs.get('relocation_ok') else 'no'}")

    if prefs.get("salary_target_usd"):
        lines.append(f"💰 target: ${prefs['salary_target_usd']}")

    lines.append(f"🎯 categories: {', '.join(profile.get('focus_categories') or [])}")
    lines.append("")
    lines.append("Save this as your active profile? (yes / no)")

    return "\n".join(lines)


def _read_resume(source: str) -> tuple[bytes, str]:
    """Résumé bytes from a path, or from stdin when the argument is `-`."""
    if source == "-":
        return sys.stdin.buffer.read(), ""

    path = Path(source)

    return path.read_bytes(), path.name


def run(args) -> int:
    """`python -m src onboard` — résumé in, active profile out."""
    try:
        data, name = _read_resume(args.resume)
    except OSError as e:
        cli.note(f"cannot read the resume: {e}")

        return 1

    text = extract_resume_text(data, name)

    if len(text) < _MIN_RESUME_CHARS:
        cli.note("too little text extracted - a scanned/image PDF cannot be read; "
                 "pipe the plain text instead")

        return 1

    profile = asyncio.run(profile_from_resume(text))

    if not is_usable_profile(profile):
        cli.note("the model did not return a usable profile - rerun, or edit profile.yaml")

        return 1

    print(format_profile_preview(profile))

    if not args.yes and input("Save? [y/N] ").strip().lower() not in {"y", "yes"}:
        cli.note("not saved")

        return 1

    config.save_active_profile(profile)
    cli.note(f"saved: {config.ACTIVE_PROFILE_PATH}")

    return 0
