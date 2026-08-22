"""Thin live canary — hits real boards. Skipped by default; run with `-m network`."""
from __future__ import annotations

import asyncio

import pytest

from src import config
from src.sources import fetch_page

pytestmark = pytest.mark.network


def _first_resource(render: str) -> dict:
    for r in config.enabled_resources():
        if r.get("render") == render:
            return r

    pytest.skip(f"no enabled resource with render={render} in resources.yaml")


@pytest.mark.parametrize("render", ["getro", "ashby", "remoteok", "consider", "jobstash"])
def test_fetch_page_returns_items(render):
    resource = _first_resource(render)
    page = asyncio.run(fetch_page(resource["url"], render))
    assert not page.get("error"), page.get("error")
    assert page.get("items"), f"{resource['name']} returned no items"
