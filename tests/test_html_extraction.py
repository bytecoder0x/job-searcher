"""Card-scoped listing extraction: the fix for silent job loss on HTML boards."""
import pytest

pytest.importorskip("selectolax")
from selectolax.parser import HTMLParser  # noqa: E402

from src.sources import html  # noqa: E402
from src.sources.html import (  # noqa: E402
    _extract_listing_text, _fit_records, _card_block,
)


def _tree(s: str) -> HTMLParser:
    return HTMLParser(s)


def test_short_cards_do_not_merge_into_one_block():
    """30 short <li> cards used to escalate to the <ul> and collapse into a
    single boundary-free line. Each must now be its own record."""
    cards = "".join(
        f'<li><a href="/jobs/solidity-engineer-conab-{i}">Solidity Engineer CoN Remote</a></li>'
        for i in range(30)
    )
    text = _extract_listing_text(_tree(f"<ul>{cards}</ul>"), "https://board.com")
    lines = [l for l in text.split("\n") if l.strip()]
    assert len(lines) == 30, f"expected 30 records, got {len(lines)}"
    # every record carries ITS OWN url, bound to the record
    assert all(" | https://board.com/jobs/solidity-engineer-conab-" in l for l in lines)
    assert all(l.startswith("- ") for l in lines)


def test_url_is_bound_to_its_own_posting():
    doc = """
    <ul>
      <li><a href="/jobs/senior-solidity-engineer-lido">Senior Solidity Engineer - Lido - Remote</a></li>
      <li><a href="/jobs/rust-protocol-engineer-jito">Rust Protocol Engineer - Jito - Remote</a></li>
    </ul>"""
    text = _extract_listing_text(_tree(doc), "https://board.com")
    lines = [l for l in text.split("\n") if l.strip()]
    assert len(lines) == 2
    solidity = next(l for l in lines if "Solidity" in l)
    rust = next(l for l in lines if "Rust" in l)
    assert solidity.endswith("/jobs/senior-solidity-engineer-lido")
    assert rust.endswith("/jobs/rust-protocol-engineer-jito")


def test_single_card_with_two_slug_links_is_not_aborted():
    """A legitimate card can carry two slug-shaped links (title + company). The
    rejected 'count strong links' guard would have misread this as a
    collection; the size-based stop keeps the whole card."""
    doc = """
    <div class="card">
      <a href="/jobs/crypto-operations-lead-role">Crypto Operations Lead</a>
      <a href="/companies/institute-of-free-technology">Institute of Free Technology</a>
      <span>Remote · $120k</span>
    </div>"""
    text = _extract_listing_text(_tree(doc), "https://cryptocurrencyjobs.co")
    lines = [l for l in text.split("\n") if l.strip()]
    # exactly one record (started from the job link, not the company link)
    assert len(lines) == 1
    assert "Crypto Operations Lead" in lines[0]
    assert lines[0].endswith("/jobs/crypto-operations-lead-role")


def test_card_block_stops_before_a_large_container():
    big = "x " * 500  # a container far over _CARD_MAX_CHARS
    doc = f'<ul><li><a href="/jobs/a-real-role-here">Tiny</a></li><li>{big}</li></ul>'
    tree = _tree(doc)
    anchor = tree.css_first("a[href]")
    block = _card_block(anchor, "https://board.com", "board.com")
    assert len(block) <= html._CARD_MAX_CHARS
    assert "Tiny" in block
    assert "x x x" not in block  # did not swallow the sibling container


def test_fit_records_cuts_on_record_boundary_and_counts():
    text = "\n".join(f"- record {i} | https://x/{i}" for i in range(10))
    fitted, dropped = _fit_records(text, 60)
    assert dropped > 0
    # never a partial line
    assert all(l.startswith("- record") and " | https://x/" in l
               for l in fitted.split("\n") if l)
    assert dropped == 10 - len(fitted.split("\n"))


def test_fit_records_noop_when_within_budget():
    text = "- a | https://x/1\n- b | https://x/2"
    assert _fit_records(text, 10_000) == (text, 0)


# ── _is_posting_path (URL classification, the two live FAILs) ──────────────
import pytest as _pytest  # noqa: E402


@_pytest.mark.parametrize("path,expected", [
    # real postings (kept)
    ("/senior-blockchain-developer-lemon-io/149705", True),   # web3.career: slug + numeric id
    ("/jobs/senior-defi-bd-at-re7-capital", True),            # cryptojobslist
    ("/operations/chronicle-head-of-operations/", True),      # cryptocurrencyjobs
    ("/jobs/569515670-senior-pm-crypto-wallet", True),        # crypto-careers
    ("/companies/wirex-ltd/vacancies/364723/", True),         # DOU: id under a posting segment
    ("/jobs/12345", True),                                    # numeric id under /jobs/
    ("/business-compliance-manager-payments-binance/152236", True),  # web3.career real job
    ("/job/reci3dUTb56efKvDX", True),                        # Find Web3: opaque Airtable id under /job/
    # NOT postings (dropped)
    ("/jobs/engineering", False),                            # Find Web3 category word under /jobs/
    ("/jobs/defi", False),                                   # Find Web3 category word
    ("/web3-jobs-hong-kong", False),                         # web3.career location filter
    ("/web3-jobs-amsterdam", False),                         # web3.career city taxonomy
    ("/web3-jobs-api", False),                               # web3.career nav ("jobs" token)
    ("/web3-salaries/smart-contract-developer", False),      # salary table, not a posting
    ("/jobs/search/engineering-and-tech", False),            # crypto-careers category page
    ("/jobs/search", False),                                  # search/filter page
    ("/employers/671835-kava-labs-inc", False),              # crypto-careers employer profile
    ("/pages/39189-craft-the-perfect-resume", False),        # CMS content page
    ("/data-science-jobs", False),                            # web3.career tag cloud (-jobs)
    ("/community-manager-jobs", False),
    ("/remote-jobs", False),                                  # category
    ("/about", False),
    ("/companies", False),                                    # company directory
    ("/vacancies", False),                                    # category listing page
    ("/x", False),
])
def test_is_posting_path(path, expected):
    assert html._is_posting_path(path) == expected


def test_web3career_category_links_are_rejected_as_records():
    """The regression: /data-science-jobs style tag-cloud links must not become
    records (they starved real cards out of the budget)."""
    doc = """
    <div class="front-tags-inline">
      <a href="/data-science-jobs">data science</a>
      <a href="/community-manager-jobs">community manager</a>
    </div>
    <ul>
      <li><a href="/senior-solidity-engineer-lido/149705">Senior Solidity Engineer - Lido - Remote</a></li>
    </ul>"""
    text = _extract_listing_text(_tree(doc), "https://web3.career")
    lines = [l for l in text.split("\n") if l.strip()]
    assert len(lines) == 1
    assert lines[0].endswith("/senior-solidity-engineer-lido/149705")
    assert "data-science-jobs" not in text and "community-manager-jobs" not in text


def test_dou_vacancy_under_companies_is_kept():
    doc = """
    <div class="vacancy">
      <a href="/companies/wirex-ltd/vacancies/364723/?from=list">Integration Engineer at Wirex</a>
      <span>Remote · Blockchain</span>
    </div>"""
    text = _extract_listing_text(_tree(doc), "https://jobs.dou.ua")
    lines = [l for l in text.split("\n") if l.strip()]
    assert len(lines) == 1
    assert "Integration Engineer" in lines[0]
    assert lines[0].endswith("/companies/wirex-ltd/vacancies/364723/?from=list")
