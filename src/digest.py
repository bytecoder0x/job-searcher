"""Plain-text digest rendering (format-digest contract: no Markdown)."""
from __future__ import annotations

from collections.abc import Callable


def _empty_reason(page: dict, render: str, catalog: bool = False) -> str:
    """Best-effort human reason WHY a source yielded no positions this run,
    inferred from what fetch_page observed (HTTP error, block page, cookie
    wall, empty API, or nav-only HTML). `catalog` sources are fetched without a
    freshness window, so blaming the window for them would be a lie — the
    caller reports window losses separately."""
    err = page.get("error")

    if err:
        return f"fetch error: {str(err)[:90]}"

    text = (page.get("text") or "")
    low = text.lower()

    if "forbidden" in low or "error 403" in low or "access denied" in low or "403" in low[:200]:
        return "HTTP 403 — blocked at the edge/WAF by IP; retry from a VPS"

    if any(s in low for s in ("captcha", "cloudflare", "turnstile", "verify you are human")):
        return "anti-bot challenge (CAPTCHA/Cloudflare) — not scrapable from here"

    if "cookie" in low and len(text) < 800:
        return "only a cookie-consent/landing page loaded — jobs not rendered"

    if render in ("getro", "a16z", "greenhouse", "lever", "ashby", "workable",
                  "remoteok", "api"):
        return ("board has no open postings right now" if catalog
                else "source API returned 0 postings in the window")

    if len(text) < 400:
        return "no content returned (JS-only page or blocked)"

    return "page loaded but no job listings were parsed (JS-rendered or unusual layout)"


def _bullet_footer(rows: list[dict], header: str, fmt: Callable[[dict], str]) -> str:
    """Shared shape for the footers below: a header line plus one `fmt`-ed
    bullet per row, or "" when `rows` is empty — the boilerplate the four
    zero/low-yield footers all repeated."""
    if not rows:
        return ""

    return "\n".join([header] + [f"   • {fmt(d)}" for d in rows])


def _no_data_footer(stats: dict) -> str:
    """Plain-text footer flagging zero-yield sources WITH a reason each. Empty
    when all sources contributed."""
    nd = stats.get("no_data") or []
    header = f"\n\n⚠️ No data from {len(nd)}/{stats.get('sources', '?')} sources:"

    return _bullet_footer(nd, header, lambda d: f"{d.get('name')} — {d.get('reason')}")


def _truncated_footer(stats: dict) -> str:
    """Flags sources where a cap actually dropped postings — so hitting a limit
    is visible, not silent (a board that returned 200 of 350 roles used to look
    perfectly healthy)."""
    tr = stats.get("truncated") or []
    header = "\n\n✂️ Truncated — cap cut postings, raise it or narrow the source:"

    return _bullet_footer(tr, header, lambda d: f"{d.get('name')} — {d.get('dropped')} dropped ({d.get('kind')})")


def _full_block(j: dict, n: int) -> str:
    """Detailed block for one ranked match (format-digest contract). `n` is the
    1-based rank, so the owner can refer to a position by number."""
    loc = j.get("location") or j.get("remote") or ""
    line1 = f"{n}. {j.get('score', '?')} | {j.get('title', '?')} @ {j.get('company', '?')}"

    if loc:
        line1 += f" — {loc}"

    block = [line1]
    meta = []

    if j.get("salary"):
        meta.append(f"💰 {j['salary']}")

    if j.get("posted"):
        meta.append(f"🗓 {j['posted']}")

    if meta:
        block.append("   " + "   ".join(meta))

    if j.get("reason"):
        block.append(f"   ✅ why: {j['reason']}")

    if j.get("url"):
        block.append(f"   🔗 {j['url']}")

    return "\n".join(block)


def _degraded_footer(stats: dict) -> str:
    """Flags sources yielding far below their recent norm — a half-broken parser
    (still returns data, just much less) that no_data's zero-check can't see."""
    dg = stats.get("degraded") or []
    header = "\n\n📉 Yield dropped vs usual — parser may be half-broken:"

    return _bullet_footer(dg, header, lambda d: f"{d.get('name')} — {d.get('yield')} now vs ~{d.get('baseline')} usual")


def _filtered_footer(stats: dict) -> str:
    """Explains a small 'relevant' count: most of what was fetched is usually
    already-shown (dedup) — NOT a miss. Without this line, '1 relevant' reads as
    a broken scan when the scan is healthy and just isn't repeating old roles."""
    seen = stats.get("already_seen") or 0
    ruled = stats.get("rule_filtered") or 0

    if not seen and not ruled:
        return ""

    bits = []

    if seen:
        bits.append(f"{seen} already shown in earlier runs")

    if ruled:
        bits.append(f"{ruled} filtered by rules")

    return f"\n\nℹ️ Skipped: {' · '.join(bits)} (/export for full history)."


def _recall_footer(stats: dict) -> str:
    """Flags boards where the cheap extractor returned far fewer positions than
    the page had records — a genuine (not merely deduped) parse shortfall that
    would otherwise silently hide real postings."""
    sf = stats.get("shortfall") or []
    header = "\n\n🔎 Low extraction recall — model under-returned:"

    return _bullet_footer(sf, header, lambda d: f"{d.get('name')} — {d.get('kept')}/{d.get('records')} parsed")


def _render_digest(scored: list[dict], stats: dict) -> str:
    """Renders ONE numbered list, best→worst, of the RELEVANT matches only
    (score >= threshold, capped at top_n).

    Deliberately does NOT dump every scored position: a full dump ran to 140+
    entries and buried the good roles under sales/PM/IT-support noise. The
    complete ranking is still persisted and available via /export.
    Always appends _no_data_footer so a failed source is never invisible."""
    footer = (_filtered_footer(stats) + _no_data_footer(stats)
              + _truncated_footer(stats) + _degraded_footer(stats)
              + _recall_footer(stats))
    threshold = stats.get("threshold", 60)
    top_n = stats.get("top_n", 15)
    ranked = sorted(scored, key=lambda j: j.get("score") or 0, reverse=True)
    relevant = [j for j in ranked if (j.get("score") or 0) >= threshold]
    shown = relevant[:top_n]

    if not shown:
        return "🔍 No new relevant positions." + footer

    header = (
        f"🎯 {len(relevant)} relevant of {len(ranked)} scored · "
        f"last {stats.get('days', '?')}d · {stats.get('sources', '?')} sources"
    )
    parts = [header]
    parts.extend(_full_block(j, n) for n, j in enumerate(shown, 1))
    # Distinguish two different reasons a match isn't printed: capped-but-still-
    # relevant (raise top_n or /export to see it) vs genuinely below threshold.
    capped, weak = len(relevant) - len(shown), len(ranked) - len(relevant)
    hidden = []

    if capped:
        hidden.append(f"{capped} more above the top-{top_n} cap")

    if weak:
        hidden.append(f"{weak} weaker, below threshold")

    if hidden:
        parts.append(f"({'; '.join(hidden)} — /export for full ranking)")

    return "\n\n".join(parts) + footer
