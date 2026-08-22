"""Runtime overrides layer: sources/meta/profile
edits persisted to overrides.json, merged on top of resources.yaml /
profile.yaml at read time. See docs/DECISIONS.md D15 for the merge rules."""
from __future__ import annotations

import copy
import json
from pathlib import Path

# Same BASE_DIR computation as config.py, kept independent (not imported from
# config) to avoid a circular import — config re-exports the names below.
BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
OVERRIDES_PATH = DATA_DIR / "overrides.json"  # runtime settings, edited by the CLI


def load_overrides() -> dict:
    """Graceful: a missing/corrupt file just means no overrides, never raise."""
    try:
        with open(OVERRIDES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_overrides(data: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)

    with open(OVERRIDES_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def update_override(section: str, key: str, value) -> None:
    data = load_overrides()
    data.setdefault(section, {})[key] = value
    save_overrides(data)


def clear_overrides() -> None:
    save_overrides({})


def clear_profile_overrides() -> None:
    """Drops only the profile-edit section (used on re-onboarding: the previous
    person's chat edits don't belong to a freshly-imported profile). Leaves
    sources/meta overrides intact."""
    data = load_overrides()

    if data.pop("profile", None) is not None:
        save_overrides(data)


# ── Profile edits (`profile --set/--add/--remove`) ───────────────────────
# Human field name → (dotted path in profile.yaml, kind). "scalar" fields only
# take `set`; "list" fields only take `add`/`remove` — enforced in
# apply_profile_edits so a stray op can never corrupt the merged profile.
PROFILE_FIELDS: dict[str, tuple[str, str]] = {
    "must_have_skill":    ("skills.must_have",              "list"),
    "nice_to_have_skill": ("skills.nice_to_have",            "list"),
    "min_salary":         ("preferences.min_salary_usd",     "scalar"),
    "salary_target":      ("preferences.salary_target_usd", "scalar"),
    "work_format":        ("preferences.work_format",        "list"),
    "relocation_ok":      ("preferences.relocation_ok",      "scalar"),
    "seniority":          ("identity.seniority",             "scalar"),
    "exclude_keyword":    ("exclude.keywords",                "list"),
    "focus_category":     ("focus_categories",                "list"),
}

FIELD_LABELS = {
    "must_have_skill": "must_have skills",
    "nice_to_have_skill": "nice_to_have skills",
    "min_salary": "min_salary",
    "salary_target": "salary_target",
    "work_format": "work_format",
    "relocation_ok": "relocation_ok",
    "seniority": "seniority",
    "exclude_keyword": "exclude keywords",
    "focus_category": "focus_categories",
}


def _ci_index(values: list, target) -> int | None:
    """Case-insensitive index of target in a list of strings, or None."""
    needle = str(target).strip().lower()

    for i, v in enumerate(values):
        if str(v).strip().lower() == needle:
            return i

    return None


def apply_profile_edits(ops: list[dict]) -> list[str]:
    """Validates and merges chat-requested profile edits (post-confirm) into
    overrides["profile"]; invalid ops are skipped, not raised, and reported
    in the returned summary. See docs/DECISIONS.md D15 for conflict rules."""
    data = load_overrides()
    section = data.setdefault("profile", {})
    section.setdefault("set", {})
    section.setdefault("add", {})
    section.setdefault("remove", {})
    summaries = []

    for op in ops:
        action, field, value = op.get("op"), op.get("field"), op.get("value")
        spec = PROFILE_FIELDS.get(field)

        if not spec or action not in ("set", "add", "remove"):
            summaries.append(f"⚠️ skipped: unknown field '{field}'")
            continue

        _, kind = spec
        label = FIELD_LABELS.get(field, field)

        if kind == "scalar":
            if action != "set":
                summaries.append(f"⚠️ skipped: '{field}' only supports 'set'")
                continue

            section["set"][field] = value
            summaries.append(f"set {label} = {value}")
            continue

        if action not in ("add", "remove"):
            summaries.append(f"⚠️ skipped: '{field}' only supports 'add'/'remove'")
            continue

        value = str(value).strip()

        if not value:
            summaries.append(f"⚠️ skipped: empty value for '{field}'")
            continue

        add_list = section["add"].setdefault(field, [])
        rem_list = section["remove"].setdefault(field, [])

        if action == "add":
            i = _ci_index(rem_list, value)

            if i is not None:
                rem_list.pop(i)

            if _ci_index(add_list, value) is None:
                add_list.append(value)

            summaries.append(f"add '{value}' to {label}")
        else:
            i = _ci_index(add_list, value)

            if i is not None:
                add_list.pop(i)

            if _ci_index(rem_list, value) is None:
                rem_list.append(value)

            summaries.append(f"remove '{value}' from {label}")

    save_overrides(data)

    return summaries


def _dotted_parent(root: dict, path: str) -> tuple[dict, str]:
    """Walks a dotted path (creating dicts as needed) → (parent, leaf_key)."""
    parts = path.split(".")
    node = root

    for p in parts[:-1]:
        node = node.setdefault(p, {})

    return node, parts[-1]


def _list_at(root: dict, path: str) -> list:
    """The list at a dotted path: missing/empty coerces to []; an existing
    non-empty non-list scalar (e.g. a bare string) is wrapped into a
    single-element list instead of being dropped."""
    node, key = _dotted_parent(root, path)
    cur = node.get(key)

    if not isinstance(cur, list):
        cur = [cur] if cur else []
        node[key] = cur

    return cur


def _apply_profile_overrides(profile: dict) -> dict:
    """Applies overrides["profile"] onto a deep copy of profile.yaml (never
    mutates the original). Caller wraps this in try/except — see load_profile."""
    section = load_overrides().get("profile") or {}
    profile = copy.deepcopy(profile)

    for field, value in (section.get("set") or {}).items():
        path, kind = PROFILE_FIELDS.get(field, (None, None))

        if path and kind == "scalar":
            node, key = _dotted_parent(profile, path)
            node[key] = value

    for field, values in (section.get("add") or {}).items():
        path, kind = PROFILE_FIELDS.get(field, (None, None))

        if not path or kind != "list":
            continue

        current = _list_at(profile, path)

        for v in values:
            if _ci_index(current, v) is None:
                current.append(v)

    for field, values in (section.get("remove") or {}).items():
        path, kind = PROFILE_FIELDS.get(field, (None, None))

        if not path or kind != "list":
            continue

        current = _list_at(profile, path)
        drop = {str(v).strip().lower() for v in values}
        current[:] = [v for v in current if str(v).strip().lower() not in drop]

    return profile


def profile_overrides_summary() -> list[str]:
    """Human lines describing the active profile overrides (/config, /profile)."""
    section = load_overrides().get("profile") or {}
    lines = []

    for field, value in (section.get("set") or {}).items():
        lines.append(f"set {FIELD_LABELS.get(field, field)} = {value}")

    for field, values in (section.get("add") or {}).items():
        for v in values:
            lines.append(f"+ {v} ({FIELD_LABELS.get(field, field)})")

    for field, values in (section.get("remove") or {}).items():
        for v in values:
            lines.append(f"- {v} ({FIELD_LABELS.get(field, field)})")

    return lines
