"""Static-HTML text/link extraction for job-listing pages (no browser, no LLM)."""
from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

if TYPE_CHECKING:
    from selectolax.parser import HTMLParser

MAX_TEXT_CHARS = 10000      # per-source char budget sent to the cheap model
                            # (raised 6000→10000 2026-08-01 to scan more of the
                            # busy boards that were truncating; costs a few more
                            # haiku tokens per source, still bounded)
MAX_LINKS = 60

# A single listing CARD's text stays under this ceiling; a pathologically long
# card is stopped here as a secondary guard (the primary card/container signal
# is the posting-link count — see _card_block).
_CARD_MAX_CHARS = 600
# Pure runaway guard, set far above what fits the char budget so the COUNTED
# char-budget trim (_fit_records) is the single binding limit — a low block cap
# would silently drop records the truncation counter can't see.
_MAX_BLOCKS = 300


def _extract_text(html: str, base_url: str = "") -> str:
    # selectolax flatten is the primary path (trafilatura strips link lists as
    # "navigation", which is exactly the signal a listing page needs);
    # trafilatura stays as a fallback for sparse/one-off pages.
    text, listing_text = "", ""

    try:
        from selectolax.parser import HTMLParser
        tree = HTMLParser(html)

        # strip script/style first — on JS-heavy (Next.js/RSC) pages, raw
        # <script> contents can fill the whole MAX_TEXT_CHARS budget
        for node in tree.css("script, style, noscript"):
            node.decompose()

        if base_url:
            listing_text = _extract_listing_text(tree, base_url)

        text = tree.text(separator="\n")
    except Exception:
        text = ""

    # some boards (web3.career, cryptojobs.com) render a huge mega-menu before
    # the listing; card-scoped extraction is denser (real postings only) when
    # it found enough, so prefer it over the plain flatten
    if len(listing_text.strip()) >= 200:
        return listing_text

    if len(text.strip()) >= 400:
        return text

    try:
        import trafilatura
        t = trafilatura.extract(html, include_links=False, favor_recall=True)

        if t:
            return t
    except Exception:
        pass

    return text or html


_LINK_KEYWORDS = ("job", "career", "position", "vacan", "gh_jid", "opening",
                  "/o/", "listing", "role", "/jobs/", "/remote-jobs/")
# navigation segments that are definitely NOT job postings
_NAV_SEGMENTS = {
    "login", "signin", "sign_in", "signup", "sign_up", "register", "about",
    "pricing", "blog", "faq", "terms", "privacy", "contact", "advertise",
    "api", "talent", "salaries", "categories", "companies", "post-job",
    "post-a-job", "hire", "users", "search", "profile", "dashboard", "help",
    "employers", "pages", "products",   # directories/CMS, never a single posting
}


def _looks_like_listing(path: str) -> bool:
    """A slug-like path segment (>=2 dashes) — typical of a job posting.
    Some boards (e.g. web3.career: /<title-slug>/<id>) put a bare numeric ID
    last; in that case check the segment before it instead."""
    segs = [s for s in path.rstrip("/").split("/") if s]

    if not segs:
        return False

    seg = segs[-1].lower()

    if seg.isdigit() and len(segs) >= 2:
        seg = segs[-2].lower()

    if not seg or seg in _NAV_SEGMENTS:
        return False

    return seg.count("-") >= 2


# Path segments that mark a real posting even without a title slug — the id
# comes separately (DOU: /companies/<co>/vacancies/<id>; /jobs/<id>).
_POSTING_SEGMENTS = {"vacancies", "vacancy", "jobs", "job",
                     "positions", "position", "opening", "openings"}


def _has_posting_segment(path: str) -> bool:
    """True when a _POSTING_SEGMENTS word appears as a DIRECTORY (not the final
    slug) — i.e. the board namespaces its postings under /jobs/, /vacancies/…
    When such links exist we trust ONLY them and drop bare root-level slugs,
    which otherwise sneak through the generic ≥2-dash slug test even though they
    are employer profiles, blog posts or CMS pages (crypto-careers:
    /employers/<id>-<co>, /is-crypto-web3-dead-2023)."""
    segs = [s for s in path.strip("/").split("/") if s]
    return any(s.lower() in _POSTING_SEGMENTS for s in segs[:-1])


def _is_posting_path(path: str) -> bool:
    """Whether a same-host path points to ONE job posting. Layers three fixes on
    top of _looks_like_listing (both found by live end-to-end testing):
    (1) reject taxonomy/category pages whose slug ends in '-jobs'/'-job'
        (web3.career's /data-science-jobs tag cloud was read as postings and
        starved the real cards out of the budget);
    (2) accept a numeric id under a posting segment (DOU's
        /companies/<co>/vacancies/<id> — no title slug in the URL);
    (3) the leading nav-segment cut (/about, /companies directory) is skipped
        when a posting segment appears deeper, so (2) isn't blanket-killed."""
    segs = [s for s in path.strip("/").split("/") if s]

    if not segs:
        return False

    segs_l = [s.lower() for s in segs]
    last = segs_l[-1]

    # A nav/filter directory with NO posting segment after it marks a listing/
    # search/category page, not one posting: crypto-careers /jobs/search/<cat>
    # (search is nav, nothing posting-ish follows). The "after it" clause keeps
    # DOU /companies/<co>/vacancies/<id> alive (companies is nav, but a posting
    # segment follows). This generalises the old first-segment-only nav cut.
    for i, s in enumerate(segs_l[:-1]):
        if s in _NAV_SEGMENTS and not any(x in _POSTING_SEGMENTS for x in segs_l[i + 1:]):
            return False

    if last in _NAV_SEGMENTS:
        return False

    # "jobs" as a TOKEN in the final slug marks a taxonomy/filter page, not one
    # posting: "/data-science-jobs" (category) and "/web3-jobs-hong-kong",
    # "/web3-jobs-amsterdam", "/web3-jobs-api" (web3.career's location/city/nav
    # filters, which inflated a listing with ~30 non-jobs). A real posting's slug
    # is role+company and never carries the word "jobs".
    if "jobs" in last.split("-") or last.endswith("-job"):
        return False

    if any("salar" in s for s in segs_l):       # /web3-salaries/<role> salary tables
        return False

    # An id directly under a posting segment IS a posting even without a title
    # slug: DOU /vacancies/<digits>, and opaque record ids like Find Web3
    # /job/reci3dUTb56efKvDX (Airtable) or UUIDs — which _looks_like_listing
    # rejects for having <2 dashes. A category word under /jobs/ (Find Web3
    # /jobs/engineering) is NOT id-like (no digit), so it stays rejected.
    if len(segs) >= 2 and segs_l[-2] in _POSTING_SEGMENTS and _looks_like_id(last):
        return True

    return _looks_like_listing(path)


def _looks_like_id(seg: str) -> bool:
    """A posting id rather than a category word: all digits, or an opaque
    alphanumeric token (≥8 chars, contains a digit) like an Airtable/UUID id."""
    if seg.isdigit():
        return True

    core = seg.replace("-", "")

    return len(core) >= 8 and core.isalnum() and any(c.isdigit() for c in core)


def _extract_links(html: str, base_url: str) -> list[str]:
    """Job-looking links on the page, capped at MAX_LINKS. `strong` (slug-
    shaped path — a real posting) is listed before `weak` (keyword match but
    not slug-shaped, e.g. a `*-jobs` taxonomy page) so a big nav/taxonomy
    ahead of the listing can't fill the whole cap before a real posting."""
    try:
        from selectolax.parser import HTMLParser
    except Exception:
        return []

    host = urlparse(base_url).netloc
    seen: set[str] = set()
    strong, weak = [], []

    for a in HTMLParser(html).css("a"):
        href = a.attributes.get("href")

        if not href:
            continue

        full = urljoin(base_url, href)
        p = urlparse(full)

        if p.netloc != host or full in seen:
            continue

        # path+query only — `full` also carries the scheme+host, and hosts like
        # web3.career / jobs.lever.co make the keyword test always true
        low = f"{p.path}?{p.query}".lower()
        first_seg = p.path.strip("/").split("/", 1)[0].lower()

        if _is_posting_path(p.path):
            seen.add(full)
            strong.append(full)
        elif first_seg not in _NAV_SEGMENTS and any(k in low for k in _LINK_KEYWORDS):
            seen.add(full)
            weak.append(full)

    return (strong + weak)[:MAX_LINKS]


_CHROME_TAGS = {"nav", "header", "footer"}


def _in_chrome(anchor) -> bool:
    """True if the link sits inside site chrome (<nav>/<header>/<footer>).
    Those hold menu/taxonomy links whose slugs (e.g. /post-web3-job,
    /jobs-that-pay-in-crypto) pass _looks_like_listing but are not postings;
    live-measured, they were stealing ~half the text budget from real cards."""
    node = anchor.parent
    depth = 0

    while node is not None and depth < 12:
        if (node.tag or "").lower() in _CHROME_TAGS:
            return True

        node = node.parent
        depth += 1

    return False


def _posting_anchors(node, base_url: str, host: str) -> int:
    """Count DISTINCT job postings linked inside `node` (by URL path), using the
    SAME filter that seeds a record (on-host, slug-shaped, not a nav segment).
    >1 means this ancestor wraps more than one posting — the list container, not
    a card. Counting distinct URLs (not raw anchors) is essential: a single card
    routinely carries two links to the SAME posting (an image link + a title
    link, the image one text-less), and counting both would mis-flag the card as
    a container and abort card extraction at the empty image anchor (crypto-
    careers rendered 0 records this way). Early-exits at 2 distinct."""
    seen: set[str] = set()

    for a in node.css("a[href]"):
        href = a.attributes.get("href")

        if not href:
            continue

        p = urlparse(urljoin(base_url, href))

        if p.netloc != host or not _is_posting_path(p.path):
            continue

        seen.add(p.path.rstrip("/"))

        if len(seen) > 1:
            return 2

    return len(seen)


def _card_block(anchor, base_url: str, host: str) -> str:
    """Climb from a job link to the LARGEST ancestor that still wraps exactly
    ONE posting — that ancestor is the card; the next one up is the list
    container. Stopping on posting-link COUNT (not raw text size) fixes both
    failure modes the reviews surfaced: a fixed size ceiling let a container of
    many terse cards (<600 chars total) merge every posting into one block,
    while counting *all* slug links mis-flagged a single card carrying a
    title+company link as a container. Counting only posting-shaped links
    (company/nav links are excluded by the same seed filter) threads both. A
    secondary size ceiling still guards a pathologically long single card."""
    node = anchor
    best = " ".join(anchor.text(separator=" ").split())   # fallback: the link's own text

    for _ in range(6):
        parent = node.parent

        if parent is None:
            break

        if _posting_anchors(parent, base_url, host) > 1:
            break                               # parent is the container — stop

        ptext = " ".join(parent.text(separator=" ").split())

        if len(ptext) > _CARD_MAX_CHARS:
            break                               # pathologically long card — stop

        node, best = parent, ptext

    return best


def _extract_listing_text(tree: "HTMLParser", base_url: str) -> str:
    """For listing pages the useful signal (title/company/meta) sits in a small
    card around each posting's link, while the page's linear text buries it
    under a mega-menu rendered first in the DOM. From each slug-shaped
    ('strong', see _extract_links) job link, take its card (_card_block) and
    emit `- {card text} | {url}` — one record per line, with the URL BOUND to
    its own posting so the extractor never has to guess which candidate link
    belongs to which record. This mirrors the `… | url` shape the Getro/ATS
    parsers already produce. Returns '' if no strong job links are found, so the
    caller falls back to the flatten."""
    host = urlparse(base_url).netloc
    seen_hrefs: set[str] = set()
    candidates: list[tuple] = []                 # (anchor, full_url, is_segmented)

    for a in tree.css("a[href]"):
        href = a.attributes.get("href")

        if not href:
            continue

        full = urljoin(base_url, href)
        p = urlparse(full)

        if p.netloc != host or full in seen_hrefs:
            continue

        if not _is_posting_path(p.path):
            continue

        if _in_chrome(a):                       # site menu/taxonomy link, not a posting
            continue

        seen_hrefs.add(full)
        candidates.append((a, full, _has_posting_segment(p.path)))

    # If the board namespaces its postings (/jobs/, /vacancies/…), trust ONLY
    # those and drop bare root slugs — that is what keeps blog/employer/CMS
    # pages out of the records on boards that mix them with real postings.
    if any(seg for _, _, seg in candidates):
        candidates = [c for c in candidates if c[2]]

    blocks: list[str] = []

    for a, full, _seg in candidates:
        text = _card_block(a, base_url, host)

        if text:
            blocks.append(f"- {text} | {full}")

        if len(blocks) >= _MAX_BLOCKS:
            break

    return "\n".join(blocks)


def _fit_records(text: str, max_chars: int) -> tuple[str, int]:
    """Truncate a newline-delimited record list to a char budget on RECORD
    boundaries (never mid-posting), returning (text, records_dropped). A raw
    `text[:max_chars]` slice cut a posting in half AND left no trace of the
    loss; keeping whole records and reporting how many didn't fit makes the
    truncation visible instead of silent."""
    if len(text) <= max_chars:
        return text, 0

    lines = text.split("\n")
    kept: list[str] = []
    size = 0

    for ln in lines:
        add = len(ln) + (1 if kept else 0)

        if size + add > max_chars:
            break

        kept.append(ln)
        size += add

    if not kept:
        # a single record longer than the whole budget — hand over a hard slice
        # of it rather than emitting nothing (the old text[:max] behaviour for
        # the degenerate flatten/trafilatura one-line case)
        return lines[0][:max_chars], len(lines) - 1

    return "\n".join(kept), len(lines) - len(kept)
