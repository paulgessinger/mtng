from unittest.mock import AsyncMock

import diskcache
import pytest
from gidgethub.aiohttp import GitHubAPI

from mtng.forge import GitHubForge
from mtng.forge import base


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch, tmp_path):
    with diskcache.Cache(str(tmp_path / "cache")) as cache:
        monkeypatch.setattr(base, "cache", cache)
        yield cache


def client(token="first-token", host="https://api.github.com"):
    return GitHubAPI(None, "mtng-tests", oauth_token=token, base_url=host)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token,host",
    [
        ("other-token", "https://api.github.com"),
        (None, "https://api.github.com"),
        ("first-token", "https://github.example/api/v3"),
    ],
)
async def test_cache_isolates_credentials_and_hosts(token, host, isolated_cache):
    first = client()
    second = client(token, host)
    first.getitem = AsyncMock(return_value={"visibility": "private"})
    second.getitem = AsyncMock(return_value={"visibility": "different"})

    assert await GitHubForge(first).getitem("/repos/team/project") == {
        "visibility": "private"
    }
    assert await GitHubForge(second).getitem("/repos/team/project") == {
        "visibility": "different"
    }
    second.getitem.assert_awaited_once()
    assert all(b"first-token" not in key for key in isolated_cache)


@pytest.mark.asyncio
async def test_cache_reuses_same_credentials_and_handles_rotation():
    gh = client()
    gh.getitem = AsyncMock(side_effect=[{"id": 1}, {"id": 2}])
    forge = GitHubForge(gh)

    assert await forge.getitem("/repos/team/project") == {"id": 1}
    assert await GitHubForge(gh).getitem("/repos/team/project") == {"id": 1}
    assert gh.getitem.await_count == 1

    gh.oauth_token = "rotated-token"
    assert await forge.getitem("/repos/team/project") == {"id": 2}
    assert gh.getitem.await_count == 2


@pytest.mark.asyncio
async def test_reviews_follow_pagination_and_cache_all_pages():
    gh = client()
    url = "https://api.github.com/repos/team/project/pulls/1/reviews"
    next_url = f"{url}?page=2"
    user = {"login": "reviewer", "html_url": "https://github.com/reviewer"}
    # Exercise gidgethub's actual pagination loop without making HTTP requests.
    gh._make_request = AsyncMock(
        side_effect=[
            ([{"user": user, "state": "COMMENTED"}] * 30, next_url, 200),
            ([{"user": user, "state": "APPROVED"}], None, 200),
        ]
    )
    forge = GitHubForge(gh)

    reviews = await forge.get_reviews(url)
    assert len(reviews) == 31
    assert reviews[-1].state == "APPROVED"
    assert gh._make_request.await_args_list[1].args[1] == next_url
    assert await forge.get_reviews(url) == reviews
    assert gh._make_request.await_count == 2


@pytest.mark.asyncio
async def test_open_pr_collection_uses_fixture_payloads():
    import json
    from pathlib import Path

    raw = json.loads((Path(__file__).parent / "ref" / "merged_prs.json").read_text())[0]
    search_item = dict(raw, pull_request={"url": raw["url"]})
    gh = client()
    gh._make_request = AsyncMock(
        side_effect=[
            ({"items": [search_item]}, None, 200),
            (raw, None, 200),
            (raw["reviews"], None, 200),
        ]
    )
    prs = await GitHubForge(gh).open_prs("acts-project/acts")
    assert len(prs) == 1 and prs[0].number == raw["number"]
    assert prs[0].reviews[0].user.login == raw["reviews"][0]["user"]["login"]
    assert gh._make_request.await_count == 3
