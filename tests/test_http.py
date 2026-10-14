import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from mtng.forge.http import CachedGitHubAPI, HttpTransport, RateLimitError, request_key
from mtng.forge import http
from test_forge_gitlab import Response, Session

URL = "https://api.example/projects/a%2Fb?page=1"
HEADERS = {"Authorization": "Bearer secret"}


@pytest.mark.asyncio
async def test_fresh_hit_and_credential_isolation(isolated_http_cache):
    session = Session([Response(200, {"a": 1}), Response(200, {"a": 2})])
    transport = HttpTransport(session)
    assert json.loads((await transport.request("GET", URL, HEADERS))[2]) == {"a": 1}
    assert json.loads((await transport.request("GET", URL, HEADERS))[2]) == {"a": 1}
    assert json.loads(
        (await transport.request("GET", URL, {"Authorization": "other"}))[2]
    ) == {"a": 2}
    assert len(session.calls) == 2 and transport.stats["hits"] == 1
    assert all(
        "secret" not in key and "secret" not in isolated_http_cache[key]
        for key in isolated_http_cache
    )
    assert all(isinstance(isolated_http_cache[key], str) for key in isolated_http_cache)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "validator,request_header",
    [("ETag", "If-None-Match"), ("Last-Modified", "If-Modified-Since")],
)
async def test_revalidation_preserves_body_and_pagination(validator, request_header):
    session = Session(
        [
            Response(
                200,
                [1],
                {validator: "v1", "Link": '<https://api.example/p?page=2>; rel="next"'},
            ),
            Response(304),
        ]
    )
    transport = HttpTransport(session, max_age=0)
    await transport.request("GET", URL, HEADERS)
    status, headers, body = await transport.request("GET", URL, HEADERS)
    assert status == 200 and json.loads(body) == [1]
    assert headers["link"].endswith('rel="next"')
    assert session.calls[1][1]["headers"][request_header] == "v1"
    assert transport.stats["revalidated"] == 1


@pytest.mark.asyncio
async def test_changed_response_replaces_stale_data():
    session = Session(
        [Response(200, {"v": 1}, {"ETag": "1"}), Response(200, {"v": 2}, {"ETag": "2"})]
    )
    transport = HttpTransport(session, max_age=0)
    await transport.request("GET", URL, HEADERS)
    assert json.loads((await transport.request("GET", URL, HEADERS))[2]) == {"v": 2}
    transport.max_age = 300
    assert json.loads((await transport.request("GET", URL, HEADERS))[2]) == {"v": 2}
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_failure_does_not_return_stale_success():
    session = Session([Response(200, {"v": 1}, {"ETag": "1"}), Response(401)])
    transport = HttpTransport(session, max_age=0)
    await transport.request("GET", URL, HEADERS)
    assert (await transport.request("GET", URL, HEADERS))[0] == 401


@pytest.mark.asyncio
@pytest.mark.parametrize("headers", [{"Cache-Control": "no-store"}, {"Vary": "*"}])
async def test_no_store(headers, isolated_http_cache):
    session = Session([Response(200, {}, headers)])
    await HttpTransport(session).request("GET", URL, HEADERS)
    assert len(isolated_http_cache) == 0


@pytest.mark.asyncio
async def test_cache_disable_never_reads_or_writes(isolated_http_cache):
    session = Session([Response(200, {}), Response(200, {})])
    transport = HttpTransport(session, enabled=False)
    await transport.request("GET", URL, HEADERS)
    await transport.request("GET", URL, HEADERS)
    assert len(session.calls) == 2 and len(isolated_http_cache) == 0


def test_keys_include_host_query_representation_and_credentials():
    key = request_key("GET", URL, HEADERS)
    assert key == request_key("GET", URL, HEADERS | {"If-None-Match": "1"})
    assert key != request_key("GET", URL.replace("example", "other"), HEADERS)
    assert key != request_key("GET", URL.replace("page=1", "page=2"), HEADERS)
    assert key != request_key("GET", URL, HEADERS | {"Accept": "other"})
    assert request_key("GET", URL + "&a=b", HEADERS) == request_key(
        "GET", URL.replace("?page=1", "?a=b&page=1"), HEADERS
    )


@pytest.mark.asyncio
async def test_concurrent_identical_requests_share_one_call():
    session = Session([Response(200, {"a": 1})])
    first, second = HttpTransport(session), HttpTransport(session)
    results = await asyncio.gather(
        first.request("GET", URL, HEADERS), second.request("GET", URL, HEADERS)
    )
    assert results[0] == results[1] and len(session.calls) == 1
    assert second.stats["shared"] == 1


@pytest.mark.asyncio
async def test_rate_limit_exceeding_wait_budget_stops(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(http.asyncio, "sleep", sleep)
    session = Session([Response(429, {}, {"Retry-After": "120"})])
    with pytest.raises(RateLimitError, match="wait budget"):
        await HttpTransport(session, max_wait=1).request("GET", URL, HEADERS)
    assert len(session.calls) == 1
    sleep.assert_not_called()


@pytest.mark.asyncio
async def test_github_transport_caches_search_and_review_pages():
    headers = {"Content-Type": "application/json"}
    session = Session(
        [
            Response(
                200,
                {"items": [{"id": 1}]},
                headers
                | {"Link": '<https://api.github.com/search/issues?page=2>; rel="next"'},
            ),
            Response(200, {"items": [{"id": 2}]}, headers),
        ]
    )
    gh = CachedGitHubAPI(session, "test", oauth_token="secret")
    for _ in range(2):
        assert [item async for item in gh.getiter("/search/issues")] == [
            {"id": 1},
            {"id": 2},
        ]
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_github_refuses_cross_origin_pagination():
    session = Session(
        [
            Response(
                200,
                [],
                {
                    "Content-Type": "application/json",
                    "Link": '<https://evil.example/next>; rel="next"',
                },
            )
        ]
    )
    gh = CachedGitHubAPI(session, "test", oauth_token="secret")
    with pytest.raises(ValueError, match="another origin"):
        [item async for item in gh.getiter("/items")]
    assert len(session.calls) == 1
