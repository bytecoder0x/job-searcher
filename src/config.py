"""Config: environment variables, paths, loading resources.yaml / profile.yaml.
Runtime-overrides layer lives in overrides.py, re-exported here for call sites."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .overrides import (  # noqa: F401 — re-exported for command/test call sites
    FIELD_LABELS, PROFILE_FIELDS, _apply_profile_overrides, apply_profile_edits,
    clear_overrides, clear_profile_overrides, load_overrides,
    profile_overrides_summary, save_overrides, update_override,
)

# Project root = parent folder of src/
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
EXPORT_DIR = DATA_DIR / "exports"  # generated CSV files

load_dotenv(BASE_DIR / ".env")


# ── Main/matching agent model ────────────────────────────────────────────
MAIN_MODEL = os.getenv("MAIN_MODEL", "sonnet")
EXTRACT_MODEL = os.getenv("EXTRACT_MODEL", "haiku")  # cheap, for draft work


def auth_mode() -> str:
    """How LLM calls are billed. `ANTHROPIC_API_KEY` in the env → per-token API
    billing (lets anyone run their own copy on their own key); otherwise the
    Claude Code SUBSCRIPTION via `claude login`. The SDK/CLI honours the env var
    itself — we only surface which mode is active."""
    return "api_key" if os.getenv("ANTHROPIC_API_KEY") else "subscription"


def load_resources() -> dict:
    with open(BASE_DIR / "resources.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


# User-added sources (`sources --add`) live here, separate from the curated
# resources.yaml catalog, so a deployment can extend the source list without
# editing the checked-in file. Both feed all_resources().
CUSTOM_SOURCES_PATH = DATA_DIR / "sources.custom.yaml"


def load_custom_sources() -> list[dict]:
    """User-added source dicts. Graceful: missing/corrupt/partial → []."""
    try:
        with open(CUSTOM_SOURCES_PATH, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except (FileNotFoundError, OSError, yaml.YAMLError):
        return []

    srcs = data.get("resources") if isinstance(data, dict) else data

    return [s for s in (srcs or []) if isinstance(s, dict) and s.get("name") and s.get("url")]


def save_custom_sources(sources: list[dict]) -> None:
    ensure_dirs()

    with open(CUSTOM_SOURCES_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump({"resources": sources}, f, allow_unicode=True, sort_keys=False)


def add_custom_source(resource: dict) -> None:
    """Adds/replaces a custom source (by case-insensitive name)."""
    name = resource["name"].strip().lower()
    kept = [s for s in load_custom_sources() if s.get("name", "").strip().lower() != name]
    kept.append(resource)
    save_custom_sources(kept)


def remove_custom_source(name: str) -> bool:
    """Removes a custom source by name. True if one was removed."""
    target = name.strip().lower()
    srcs = load_custom_sources()
    kept = [s for s in srcs if s.get("name", "").strip().lower() != target]

    if len(kept) == len(srcs):
        return False

    save_custom_sources(kept)

    return True


def all_resources() -> list[dict]:
    """Curated catalog (resources.yaml) + user-added custom sources, deduped by
    case-insensitive name (custom wins) — the single source of truth for which
    boards exist. The stored dict keeps its original-cased `name` for display;
    only the dedup key is normalised, matching add_custom_source's lookup."""
    by_name: dict[str, dict] = {}

    for r in load_resources().get("resources", []):
        if r.get("name"):
            by_name[r["name"].strip().casefold()] = dict(r)

    for s in load_custom_sources():
        by_name[s["name"].strip().casefold()] = dict(s)   # custom overrides a same-named catalog entry

    return list(by_name.values())


def enabled_resources(categories: list[str] | None = None) -> list[dict]:
    """Enabled resources (catalog + custom), optionally filtered by category.
    Per-board /source on|off overrides are applied by exact name first."""
    resources = all_resources()
    source_overrides = load_overrides().get("sources", {})

    for r in resources:
        if r.get("name") in source_overrides:
            r["enabled"] = source_overrides[r["name"]]

    items = [r for r in resources if r.get("enabled")]

    if categories:
        cats = set(categories)
        items = [r for r in items if r.get("category") in cats]

    return items


# Company career boards (ATS) publish a CATALOG of every currently-open role,
# not a feed of new postings. A "last N days" window discards most of them —
# Uniswap/Alchemy/Circle each had 10-17 open roles but 0 posted in the last week,
# so the window emptied the board entirely. Dedup already guarantees a position
# is shown once ever, so catalogs skip the window instead.
# `consider`/`jobstash` are deliberately NOT here: both expose a structured
# posting date (timeStamp / timestamp) and a server-side date filter
# (postedSince / publicationDate), so — like getro/a16z/remoteok — they are
# dated feeds windowed normally, not catalogs.
CATALOG_RENDERS = {"greenhouse", "lever", "ashby", "workable"}


def is_catalog(resource: dict) -> bool:
    """Whether a source lists all open roles (no freshness window) rather than a
    dated feed. Explicit `catalog:` in resources.yaml wins; otherwise inferred
    from the render family."""
    explicit = resource.get("catalog")

    if explicit is not None:
        return bool(explicit)

    return resource.get("render") in CATALOG_RENDERS


def resources_meta() -> dict:
    """YAML meta, updated with any `/set`-driven overrides on top."""
    meta = dict(load_resources().get("meta", {}))
    meta.update(load_overrides().get("meta", {}))

    return meta


def score_threshold() -> int:
    """Digest score cutoff for the matcher. Graceful: 60 if unset."""
    try:
        return int(resources_meta().get("score_threshold", 60))
    except Exception:
        return 60


def default_window_days() -> int:
    """Default freshness window (days back). Graceful: 1 if unset."""
    try:
        return int(resources_meta().get("default_window_days", 1))
    except Exception:
        return 1


def max_window_days() -> int:
    """Maximum freshness window (days). Graceful: 7 if unset."""
    try:
        return int(resources_meta().get("max_window_days", 7))
    except Exception:
        return 7


def matcher_input_cap() -> int:
    """Ceiling on positions scored by sonnet after the prefilter. Graceful: 500 if unset."""
    try:
        return int(resources_meta().get("matcher_input_cap", 500))
    except Exception:
        return 500


# Profile resolution, most specific first:
#   data/profile.active.yaml — written by `onboard` from a résumé (the generic
#                              path: anyone runs their own copy, no file edits)
#   profile.yaml             — your own seed, kept out of version control
#   profile.example.yaml     — the checked-in template, so a fresh checkout runs
# Overrides (overrides.json) layer on top of whichever base is active.
ACTIVE_PROFILE_PATH = DATA_DIR / "profile.active.yaml"
LOCAL_PROFILE_PATH = BASE_DIR / "profile.yaml"
EXAMPLE_PROFILE_PATH = BASE_DIR / "profile.example.yaml"


def seed_profile_path() -> Path:
    """The seed base: your own profile.yaml if present, else the example."""
    return LOCAL_PROFILE_PATH if LOCAL_PROFILE_PATH.exists() else EXAMPLE_PROFILE_PATH


def profile_base_path() -> Path:
    """The onboarding-generated active profile if present, else the seed."""
    return ACTIVE_PROFILE_PATH if ACTIVE_PROFILE_PATH.exists() else seed_profile_path()


def save_active_profile(profile: dict) -> None:
    """Persists an onboarding-generated profile as the active base (never touches
    the seed)."""
    ensure_dirs()

    with open(ACTIVE_PROFILE_PATH, "w", encoding="utf-8") as f:
        yaml.safe_dump(profile, f, allow_unicode=True, sort_keys=False)


def _read_yaml_dict(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError):
        return None


def load_profile() -> dict:
    """Active profile base (see profile_base_path) with overrides merged on top
    (never mutates either file). Graceful: a corrupt/half-written active profile
    falls back to the seed (else {}); a malformed override section falls back
    to raw."""
    profile = _read_yaml_dict(profile_base_path())

    if profile is None:
        profile = _read_yaml_dict(seed_profile_path()) or {}

    try:
        return _apply_profile_overrides(profile)
    except Exception:
        return profile


def read_text(rel_path: str) -> str:
    return (BASE_DIR / rel_path).read_text(encoding="utf-8")


def ensure_dirs() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
