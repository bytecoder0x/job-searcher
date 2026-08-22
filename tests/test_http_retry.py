"""Wave 5: transient-failure retry/backoff in the fetch layer."""
from __future__ import annotations

import asyncio

import httpx
import pytest

from src.sources import http


class _Resp:
    def __init__(self, status: int):
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=None, response=None)


class _Client:
    """Yields queued outcomes (ints → status codes, Exceptions → raised)."""
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    async def get(self, url, **kw):
        self.calls += 1
        o = self.outcomes.pop(0)

        if isinstance(o, Exception):
            raise o

        return _Resp(o)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    async def _instant(_):  # keep backoff logic, skip the wall-clock wait
        return None

    monkeypatch.setattr(http.asyncio, "sleep", _instant)


def test_retries_transient_status_then_succeeds():
    client = _Client([503, 502, 200])
    r = asyncio.run(http.get_with_retry(client, "http://x", retries=3))
    assert r.status_code == 200 and client.calls == 3


def test_retries_network_error_then_succeeds():
    client = _Client([httpx.ConnectError("boom"), 200])
    r = asyncio.run(http.get_with_retry(client, "http://x", retries=3))
    assert r.status_code == 200 and client.calls == 2


def test_does_not_retry_hard_4xx():
    client = _Client([404, 200])

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(http.get_with_retry(client, "http://x", retries=3))

    assert client.calls == 1                     # 404 is a real error, not retried


def test_gives_up_after_all_retries_on_persistent_5xx():
    client = _Client([503, 503, 503])

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(http.get_with_retry(client, "http://x", retries=3))

    assert client.calls == 3


def test_reraises_network_error_after_exhausting_retries():
    client = _Client([httpx.ReadTimeout("t"), httpx.ReadTimeout("t"),
                      httpx.ReadTimeout("t")])

    with pytest.raises(httpx.TransportError):
        asyncio.run(http.get_with_retry(client, "http://x", retries=3))

    assert client.calls == 3
