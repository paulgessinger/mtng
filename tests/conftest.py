"""Ordinary tests must be deterministic and must not contact live APIs."""

import aiohttp
import pytest
import requests


@pytest.fixture(autouse=True)
def forbid_live_http(request, monkeypatch):
    if request.node.get_closest_marker("integration"):
        return

    def blocked(*args, **kwargs):
        raise AssertionError("Live HTTP is forbidden outside integration tests")

    monkeypatch.setattr(aiohttp.ClientSession, "_request", blocked)
    monkeypatch.setattr(requests.Session, "request", blocked)


@pytest.fixture(autouse=True)
def isolated_http_cache(monkeypatch, tmp_path):
    import diskcache
    from mtng.forge import http

    with diskcache.Cache(str(tmp_path / "http-cache")) as cache:
        monkeypatch.setattr(http, "cache", cache)
        yield cache
