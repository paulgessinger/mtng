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


@pytest.mark.asyncio
@pytest.mark.parametrize("directive", ["private, max-age=60", "no-cache"])
async def test_local_freshness_overrides_short_server_lifetime(monkeypatch, directive):
    now = 1000.0
    monkeypatch.setattr(http.time, "time", lambda: now)
    session = Session(
        [
            Response(200, {"v": 1}, {"Cache-Control": directive, "ETag": "v1"}),
            Response(304),
        ]
    )
    transport = HttpTransport(session)
    await transport.request("GET", URL, HEADERS)
    now += 120
    await transport.request("GET", URL, HEADERS)
    assert len(session.calls) == 1
    assert transport.stats["hits"] == 1
    now += 181
    await transport.request("GET", URL, HEADERS)
    assert len(session.calls) == 2
    assert transport.stats["revalidated"] == 1


@pytest.mark.asyncio
async def test_github_overlaps_requests_but_bounds_concurrency():
    active = peak = 0
    release = asyncio.Event()
    full = asyncio.Event()

    class SlowResponse(Response):
        async def read(self):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 4:
                full.set()
            try:
                await release.wait()
                return b"{}"
            finally:
                active -= 1

    session = Session([SlowResponse(200) for _ in range(8)])
    transport = HttpTransport(session, github=True, enabled=False)
    tasks = [
        asyncio.create_task(transport.request("GET", URL + f"&id={i}", HEADERS))
        for i in range(8)
    ]
    try:
        await asyncio.wait_for(full.wait(), timeout=5)
        assert len(session.calls) == 4
    finally:
        release.set()
        await asyncio.gather(*tasks)
    assert peak == 4


@pytest.fixture
def scheduler_clock(monkeypatch):
    now = [1000.0]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(http.time, "time", lambda: now[0])
    monkeypatch.setattr(http.asyncio, "sleep", sleep)
    return now, sleeps


@pytest.mark.asyncio
async def test_github_paces_core_and_search(scheduler_clock):
    scope = http.Coordinator()
    await scope.admit(("token", "core"), 60)
    await scope.admit(("token", "core"), 60)
    await scope.admit(("token", "search"), 60)
    await scope.admit(("token", "search"), 60)
    assert scheduler_clock[1] == pytest.approx([0.2, 0.2, 2.1])


@pytest.mark.asyncio
async def test_github_reserves_quota_and_ignores_out_of_order_increases(
    scheduler_clock,
):
    scope = http.Coordinator()
    bucket = ("token", "core")
    scope.observe(bucket, {"x-ratelimit-remaining": "6", "x-ratelimit-reset": "1100"})
    await scope.admit(bucket, 60)
    scope.observe(bucket, {"x-ratelimit-remaining": "6", "x-ratelimit-reset": "1100"})
    await scope.admit(bucket, 60)
    with pytest.raises(http.RateLimitError):
        await scope.admit(bucket, 60)
    # Exhausted core quota does not block search or another credential.
    await scope.admit(("token", "search"), 60)
    await scope.admit(("other", "core"), 60)
    scheduler_clock[0][0] = 1101
    scope.observe(bucket, {"x-ratelimit-remaining": "100", "x-ratelimit-reset": "1200"})
    await scope.admit(bucket, 60)
    assert scope.budgets[bucket] == (99, 1200)


@pytest.mark.asyncio
async def test_github_throttle_cools_down_other_transports(scheduler_clock):
    session = Session(
        [Response(429, headers={"Retry-After": "2"}), Response(200), Response(200)]
    )
    first = HttpTransport(session, github=True, enabled=False)
    second = HttpTransport(session, github=True, enabled=False)
    await first.request("GET", URL, HEADERS)
    await second.request("GET", URL + "&other=1", HEADERS)
    assert scheduler_clock[1] == pytest.approx([2, 0.4])
    assert first.stats["retries"] == 1


@pytest.mark.asyncio
async def test_refresh_revalidates_even_a_fresh_local_entry():
    session = Session([Response(200, {"v": 1}, {"ETag": "v1"}), Response(304)])
    await HttpTransport(session).request("GET", URL, HEADERS)
    refresh = HttpTransport(session, max_age=0)
    await refresh.request("GET", URL, HEADERS)
    assert refresh.stats["hits"] == 0
    assert refresh.stats["revalidated"] == 1
    assert session.calls[1][1]["headers"]["If-None-Match"] == "v1"


@pytest.mark.asyncio
async def test_github_rechecks_cooldown_extended_by_another_response(monkeypatch):
    now = [1000.0]
    scope = http.Coordinator(not_before=1001)
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay
        if len(sleeps) == 1:
            scope.not_before = 1003

    monkeypatch.setattr(http.time, "time", lambda: now[0])
    monkeypatch.setattr(http.asyncio, "sleep", sleep)
    await scope.admit(("token", "core"), 60)
    assert sleeps == [1, 2]


@pytest.mark.asyncio
async def test_github_exhausted_quota_prevents_another_network_call(scheduler_clock):
    session = Session(
        [Response(200, {}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1200"})]
    )
    transport = HttpTransport(session, github=True, enabled=False)
    await transport.request("GET", URL, HEADERS)
    with pytest.raises(http.RateLimitError):
        await transport.request("GET", URL + "&other=1", HEADERS)
    assert len(session.calls) == 1
