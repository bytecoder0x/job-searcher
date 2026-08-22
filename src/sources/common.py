"""Helpers shared across source parsers: profile query terms, date parsing."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .. import config


def _profile_query_terms(limit: int = 10, include_nice: bool = False) -> list[str]:
    """Search terms from the active profile's skills, used to keep relevant roles
    when a source returns far more than we want to forward (Getro's server-side
    query, a16z/RemoteOK relevance filters). `must_have` only by default (narrow —
    Getro ANDs query words); `include_nice=True` also adds `nice_to_have`, a wider
    net for coarse relevance filtering where recall matters more than precision.
    Compound entries like 'CI / CD' are split on '/'. Graceful: no profile/skills
    → empty list (unfiltered mode)."""
    try:
        skills = config.load_profile().get("skills") or {}
    except Exception:
        return []

    groups = (skills.get("must_have") or [])

    if include_nice:
        groups = list(groups) + list(skills.get("nice_to_have") or [])

    seen, terms = set(), []

    for entry in groups:
        for part in str(entry).split("/"):
            t = part.strip()
            key = t.lower()

            if t and key not in seen:
                seen.add(key)
                terms.append(t)

    return terms[:limit]


def _iso_to_epoch(s: object) -> float | None:
    """ISO-8601 (with optional Z or date-only) → unix seconds. Graceful → None."""
    if not isinstance(s, str) or not s:
        return None

    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        try:
            return datetime.fromisoformat(s[:10]).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            return None


def _epoch_to_date(ts: float | None) -> str | None:
    if ts is None:
        return None

    try:
        return datetime.fromtimestamp(ts, tz=timezone.utc).date().isoformat()
    except (OverflowError, OSError, ValueError, TypeError):
        return None


def term_in(term: str, haystack: str) -> bool:
    """Word-boundary containment: `term in haystack` without substring
    false positives ('dex' inside 'index', 'rust' inside 'trust'). Both args
    are expected lower-cased already (callers lower the whole haystack once)."""
    return re.search(rf"\b{re.escape(term)}\b", haystack) is not None
