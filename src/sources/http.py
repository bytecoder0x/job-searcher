"""HTTP fetch + the shared Playwright browser (HTML parsing lives in html.py)."""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from playwright.async_api import Browser

log = logging.getLogger("job-searcher")

_UA = "Mozilla/5.0 (compatible; JobSearcherBot/1.0)"
# transient HTTP statuses worth a retry (rate limit + upstream hiccups); a 4xx
# other than 429 is a real client error and is NOT retried.
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


def _backoff(attempt: int) -> float:
    return 0.5 * (2 ** attempt)                 # 0.5s, 1s, 2s …


async def request_with_retry(client: httpx.AsyncClient, method: str, url: str,
                             retries: int = 3, **kw) -> httpx.Response:
    """HTTP request with exponential backoff on TRANSIENT failures
    (network/timeout, 429/5xx). A single blip used to drop a whole board for the
    run; a couple of cheap retries recover it. Non-transient 4xx raise
    immediately (no point retrying a 404). Shared by the structured parsers
    (ATS/Getro/RemoteOK) and the static HTML fetch."""
    for attempt in range(retries):
        try:
            r = await getattr(client, method.lower())(url, **kw)

            if r.status_code in _RETRY_STATUSES and attempt < retries - 1:
                log.info("sources: %s → HTTP %d, retry %d/%d",
                         url, r.status_code, attempt + 1, retries)
                await asyncio.sleep(_backoff(attempt))
                continue

            r.raise_for_status()

            return r
        except httpx.TransportError as e:       # connect/read/write/pool/timeout
            if attempt == retries - 1:
                log.warning("sources: %s failed after %d attempts: %s: %s",
                            url, retries, type(e).__name__, e)
                raise

            log.info("sources: %s → %s, retry %d/%d",
                     url, type(e).__name__, attempt + 1, retries)
            await asyncio.sleep(_backoff(attempt))

    # unreachable: every path through the loop returns or raises on the final
    # attempt (retryable-status branch falls through to raise_for_status/return,
    # TransportError branch re-raises) — kept explicit for the type checker.
    raise RuntimeError("get_with_retry: retry loop exited without return/raise")


async def get_with_retry(client: httpx.AsyncClient, url: str,
                         retries: int = 3, **kw) -> httpx.Response:
    """GET convenience wrapper over request_with_retry."""
    return await request_with_retry(client, "GET", url, retries=retries, **kw)


async def _fetch_static(url: str) -> str:
    async with httpx.AsyncClient(
        headers={"User-Agent": _UA}, follow_redirects=True, timeout=25
    ) as client:
        r = await get_with_retry(client, url)
        return r.text


# ── Shared Playwright browser ──────────────────────────────────────────────
# Lazily launched once and reused for the process lifetime: a scan fetches
# ~10 render:js sources concurrently, a fresh Chromium per call would be slow
# and heavy. See docs/DECISIONS.md.
_pw_instance = None                 # playwright.async_api.Playwright, once started
_browser: "Browser | None" = None   # launched Chromium, shared across calls
_browser_lock = asyncio.Lock()


async def _get_browser() -> "Browser | None":
    """Shared Chromium, launched on first use (double-checked locking).
    Graceful — None if playwright/chromium isn't available."""
    global _pw_instance, _browser

    if _browser is not None:
        return _browser

    async with _browser_lock:
        if _browser is not None:            # another task already launched it
            return _browser

        try:
            from playwright.async_api import async_playwright
            _pw_instance = await async_playwright().start()
            _browser = await _pw_instance.chromium.launch(headless=True)
        except Exception:
            _pw_instance = None
            _browser = None

    return _browser


async def _reset_browser() -> None:
    """Drops the shared browser/driver handles (e.g. after a crash) so the
    next _fetch_js call launches a fresh one."""
    global _pw_instance, _browser
    browser, pw = _browser, _pw_instance
    _browser = None
    _pw_instance = None

    if browser is not None:
        try:
            await browser.close()
        except Exception:
            pass

    if pw is not None:
        try:
            await pw.stop()
        except Exception:
            pass


async def close_browser() -> None:
    """Clean shutdown of the shared browser + Playwright driver."""
    await _reset_browser()


async def _fetch_js(url: str) -> str:
    """Renders JS via the shared browser: waits, scrolls, and collects HTML
    from all frames (Getro/Consider boards often load async and/or in an
    iframe). Falls back to a static fetch if playwright is unavailable or the
    shared browser turns out to be dead."""
    browser = await _get_browser()

    if browser is None:
        return await _fetch_static(url)

    context = None

    try:
        context = await browser.new_context(
            user_agent=_UA, viewport={"width": 1366, "height": 900})
        page = await context.new_page()
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)

        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass

        await page.wait_for_timeout(3500)          # let JS finish rendering the list

        try:
            # SPA boards (crypto-careers) hydrate the job list AFTER networkidle;
            # a snapshot taken too early catches only the category chrome and
            # yields 0 postings. Wait for real id-carrying postings to appear —
            # but only on boards that namespace under /jobs//vacancies/ (else the
            # condition is already satisfied, so slug-id boards pay no penalty).
            await page.wait_for_function(
                """() => {
                    const h = [...document.querySelectorAll('a[href]')]
                        .map(a => a.getAttribute('href') || '');
                    const dir = h.some(x => /\\/(jobs|vacancies|vacancy|positions)\\//i.test(x));
                    if (!dir) return true;
                    return h.filter(x => /\\/(jobs|vacancies|vacancy|positions)\\/[^/]*\\d/i.test(x)).length >= 3;
                }""",
                timeout=6000)
        except Exception:
            pass

        try:                                        # trigger lazy loading
            await page.mouse.wheel(0, 5000)
            await page.wait_for_timeout(1500)
        except Exception:
            pass

        htmls = []                                  # top + all iframes

        for fr in page.frames:
            try:
                htmls.append(await fr.content())
            except Exception:
                continue

        return "\n".join(htmls) if htmls else await page.content()
    except Exception:
        # the shared browser may have crashed/disconnected — drop it and fall
        # back this call rather than wedging the rest of the scan
        await _reset_browser()
        return await _fetch_static(url)
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
