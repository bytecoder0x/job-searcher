"""Isolated agent calls (SDK) and the JSON parsing helpers around them.
Prompts come from .claude/agents and .claude/skills (single source of truth).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re

from . import config, nowindow

log = logging.getLogger("job-searcher")

# The SDK spawns the Claude CLI as a child process per call; on Windows each one
# would flash a console window. Applied here too (not only at the command entry
# point) so scripts and tests that import llm directly are covered.
nowindow.apply()


def _load_agent(name: str) -> tuple[str, str]:
    """Reads .claude/agents/<name>.md → (model_alias, body)."""
    raw = config.read_text(f".claude/agents/{name}.md")
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)

    if not m:
        return "sonnet", raw

    import yaml
    meta = yaml.safe_load(m.group(1)) or {}

    return meta.get("model", "sonnet"), m.group(2).strip()


_SKILLS: dict[str, str] = {}


def _skill(name: str) -> str:
    """Skill body from .claude/skills/<name>/SKILL.md, cached per process."""
    if name not in _SKILLS:
        _SKILLS[name] = config.read_text(f".claude/skills/{name}/SKILL.md")

    return _SKILLS[name]


def _salvage_objects(s: str) -> list[dict]:
    """Recover top-level {...} objects one at a time from a not-quite-valid JSON
    blob (string/escape aware), parsing each independently. A single malformed
    object (unescaped quote, truncated tail) then costs ONE position instead of
    the whole source — the cheap model's near-misses used to zero out a board."""
    out: list[dict] = []
    depth = start = 0
    in_str = esc = False

    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False

            continue

        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i

            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1

            if depth == 0:
                try:
                    obj = json.loads(s[start:i + 1])

                    if isinstance(obj, dict):
                        out.append(obj)
                except Exception:
                    pass

    return out


def _parse_json_array(text: str) -> list[dict]:
    """Parse a JSON array of objects from model output. Layers cheap repairs so a
    near-miss answer keeps its good records instead of losing the whole source:
    strict parse → trailing-comma repair → per-object salvage."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    a, b = text.find("["), text.rfind("]")
    slice_ = text[a:b + 1] if a != -1 and b != -1 and b > a else text

    try:
        data = json.loads(slice_)

        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception:
        pass

    try:                                            # trailing commas: [{..},] / {..,}
        data = json.loads(re.sub(r",\s*([}\]])", r"\1", slice_))

        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
    except Exception:
        pass

    return _salvage_objects(slice_)                 # per-object recovery


def _parse_json_object(text: str) -> dict:
    """First {...} object in the text (fence-tolerant). {} on failure."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    a, b = text.find("{"), text.rfind("}")

    if a == -1 or b == -1:
        return {}

    try:
        obj = json.loads(text[a:b + 1])
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _parse_int_array(text: str) -> list[int] | None:
    """Array of ints from agent output (fence-tolerant). None means unparseable
    (distinct from a valid empty `[]`), so callers can tell "agent failed" from
    "agent legitimately kept nothing"."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.M).strip()
    a, b = text.find("["), text.rfind("]")

    if a == -1 or b == -1:
        return None

    try:
        data = json.loads(text[a:b + 1])
    except Exception:
        return None

    if not isinstance(data, list):
        return None

    return [int(x) for x in data if isinstance(x, (int, float)) and not isinstance(x, bool)]


async def _run_agent(system_prompt: str, user_prompt: str, model: str,
                     max_turns: int | None = None, resume: str | None = None,
                     ) -> tuple[str, str | None]:
    """One isolated agent call without tools → (final text, session_id).

    max_turns=None on purpose: with allowed_tools=[] the model never loops, so a
    cap only turned rate-limit retries into hard 'max turns' errors (see D13 in
    docs/DECISIONS.md). `resume` continues a prior session_id (used by _ask).
    """
    from claude_agent_sdk import query, ClaudeAgentOptions, ResultMessage

    options = ClaudeAgentOptions(
        system_prompt=system_prompt,
        model=model,
        allowed_tools=[],        # data is already in the prompt → no tools, no permission prompts
        setting_sources=[],      # hermetic: don't pull in extra settings
        max_turns=max_turns,
        resume=resume,
    )
    attempts = 3
    last = "no result"

    for attempt in range(attempts):
        result, session_id, err = "", None, None

        try:
            async for msg in query(prompt=user_prompt, options=options):
                if isinstance(msg, ResultMessage):
                    session_id = msg.session_id

                    if getattr(msg, "is_error", False):
                        err = getattr(msg, "subtype", "error")
                    elif msg.result:
                        result = msg.result

            if result and not err:
                return result, session_id

            last = err or "empty result"
        except Exception as e:                    # transport / rate-limit error
            last = f"{type(e).__name__}: {e}"

        log.warning("agent call attempt %d/%d failed: %s", attempt + 1, attempts, last)

        if attempt < attempts - 1:
            await asyncio.sleep(2 * (attempt + 1))  # brief backoff, then retry

    raise RuntimeError(f"agent call failed after {attempts} attempts: {last}")
