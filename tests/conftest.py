"""Ordinary tests must be deterministic and must not contact live APIs."""

import aiohttp
import pytest
import requests


def _is_local(node) -> bool:
    return any(
        node.get_closest_marker(name) is not None
        for name in ("integration", "local_http")
    )


@pytest.fixture(autouse=True)
def forbid_live_http(request, monkeypatch):
    # "local_http" covers tests that dial a loopback server started by the
    # test itself; no external service is contacted.
    if _is_local(request.node):
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


@pytest.fixture(autouse=True)
def isolated_user_config(monkeypatch, tmp_path):
    """Point the user config layer at an empty temp path; tests that exercise
    the layer write to ``cli.user_config_path()`` explicitly."""

    from mtng import cli

    path = tmp_path / "user-config.toml"
    monkeypatch.setattr(cli, "user_config_path", lambda: path)
    return path
