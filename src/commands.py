"""Everything that inspects or changes what a scan runs with: the profile, the
source list, and the matching settings. The scan itself lives in scan.py."""
from __future__ import annotations

import asyncio
import logging

from . import cli, config, detect

log = logging.getLogger("job-searcher")

# Setting name → (resources.yaml meta key, min, max). The ranges are guard rails:
# a typo must not silently disable matching or uncap the expensive scorer.
_SETTINGS: dict[str, tuple[str, int, int]] = {
    "threshold": ("score_threshold", 0, 100),
    "top_n": ("digest_top_n", 1, 50),
    "window": ("default_window_days", 1, 7),
    "cap": ("matcher_input_cap", 10, 500),
}


def _coerce(raw: str):
    """CLI arguments arrive as strings; profile scalars are typed."""
    text = raw.strip()
    low = text.lower()

    if low in {"true", "yes", "on"}:
        return True

    if low in {"false", "no", "off"}:
        return False

    if low in {"none", "null"}:
        return None

    try:
        return int(text)
    except ValueError:
        return text


# ── profile ────────────────────────────────────────────────────────────────
def _show_profile() -> None:
    profile = config.load_profile()
    identity = profile.get("identity") or {}
    skills = profile.get("skills") or {}
    prefs = profile.get("preferences") or {}
    overrides = config.profile_overrides_summary()

    print(f"role             : {identity.get('role', '—')}")
    print(f"seniority        : {identity.get('seniority', '—')}")
    print(f"must_have        : {', '.join(skills.get('must_have') or []) or '—'}")
    print(f"nice_to_have     : {', '.join(skills.get('nice_to_have') or []) or '—'}")
    print(f"work_format      : {', '.join(prefs.get('work_format') or []) or '—'}")
    print(f"min_salary_usd   : {prefs.get('min_salary_usd')}")
    print(f"salary_target_usd: {prefs.get('salary_target_usd')}")
    print(f"relocation_ok    : {prefs.get('relocation_ok')}")
    print(f"focus_categories : {', '.join(profile.get('focus_categories') or []) or '—'}")
    print(f"base file        : {config.profile_base_path().name}")
    print("overrides        : " + (f"{len(overrides)} active" if overrides else "none"))

    for line in overrides:
        print(f"  • {line}")


def run_profile(args) -> int:
    """Show the effective profile, or layer an override on top of it."""
    if args.fields:
        for field, (path, kind) in sorted(config.PROFILE_FIELDS.items()):
            print(f"{field:<20} {kind:<7} {path}")

        return 0

    if args.reset:
        config.clear_profile_overrides()
        cli.note("profile overrides cleared")

        return 0

    edit = next(((op, pair) for op, pair in
                 (("set", args.set), ("add", args.add), ("remove", args.remove)) if pair), None)

    if edit is None:
        _show_profile()

        return 0

    op, (field, value) = edit
    summaries = config.apply_profile_edits(
        [{"op": op, "field": field, "value": _coerce(value) if op == "set" else value}]
    )

    for line in summaries:
        print(line)

    return 0 if not any(s.startswith("⚠️") for s in summaries) else 1


# ── sources ────────────────────────────────────────────────────────────────
def _list_sources() -> None:
    overrides = config.load_overrides().get("sources", {})
    custom = {s["name"] for s in config.load_custom_sources()}

    for resource in config.all_resources():
        name = resource.get("name", "?")
        enabled = overrides.get(name, resource.get("enabled"))
        mark = "on " if enabled else "off"
        tag = " (added)" if name in custom else ""
        print(f"{mark}  {name} [{resource.get('category')}]{tag}")


def _count_sources() -> None:
    by_category: dict[str, int] = {}

    for resource in config.enabled_resources():
        by_category[resource["category"]] = by_category.get(resource["category"], 0) + 1

    print(f"enabled: {sum(by_category.values())} of {len(config.all_resources())}")

    for category, count in sorted(by_category.items()):
        print(f"  {category}: {count}")


def _add_source(url: str, category: str, name: str | None) -> int:
    cli.note(f"probing {url} …")

    try:
        found = asyncio.run(detect.detect_source(url, category, name))
    except Exception as e:                      # a bad URL must not traceback
        log.warning("detection failed", exc_info=True)
        cli.note(f"detection failed: {e}")

        return 1

    if not found:
        cli.note("no postings parsed from that URL (blocked, empty, or an unusual "
                 "layout) - try the board's listing page directly")

        return 1

    config.add_custom_source({k: found[k] for k in
                              ("name", "url", "category", "render", "enabled", "notes")})
    print(f"added '{found['name']}': {found['render']} render, "
          f"{found['detected_count']} postings, category={found['category']}")

    return 0


def run_sources(args) -> int:
    """List the boards, or add/remove/toggle one."""
    if args.add:
        return _add_source(args.add, args.category, args.name)

    if args.remove:
        if config.remove_custom_source(args.remove):
            cli.note(f"removed '{args.remove}'")

            return 0

        cli.note(f"no added source named '{args.remove}' (catalog boards use --off)")

        return 1

    for flag, enabled in ((args.on, True), (args.off, False)):
        if flag:
            config.update_override("sources", flag, enabled)
            cli.note(f"{flag}: {'on' if enabled else 'off'}")

            return 0

    _list_sources() if args.list else _count_sources()

    return 0


# ── settings ───────────────────────────────────────────────────────────────
def run_config(args) -> int:
    """Show the effective matching settings, or override one of them."""
    if args.reset:
        config.clear_overrides()
        cli.note("overrides cleared - resources.yaml defaults restored")

        return 0

    if args.set:
        key, raw = args.set
        spec = _SETTINGS.get(key.lower())

        if not spec or not raw.lstrip("-").isdigit():
            cli.note("usage: config --set KEY VALUE, where KEY is one of "
                     + ", ".join(f"{k} ({lo}-{hi})" for k, (_, lo, hi) in _SETTINGS.items()))

            return 1

        meta_key, low, high = spec
        value = int(raw)

        if not low <= value <= high:
            cli.note(f"{key} must be between {low} and {high}")

            return 1

        config.update_override("meta", meta_key, value)
        print(f"{meta_key} = {value}")

        return 0

    # The effective values the pipeline uses, not the raw YAML: an unset key
    # still has a default, and printing None there would read as "off".
    effective = {
        "threshold": config.score_threshold(),
        "top_n": config.resources_meta().get("digest_top_n", 15),
        "window": config.default_window_days(),
        "cap": config.matcher_input_cap(),
    }

    for key, (meta_key, _low, _high) in _SETTINGS.items():
        print(f"{key:<10} {effective[key]:<6} ({meta_key})")

    print(f"source overrides : {len(config.load_overrides().get('sources', {}))}")
    print(f"profile overrides: {len(config.profile_overrides_summary())}")

    return 0
