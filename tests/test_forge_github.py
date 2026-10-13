from unittest.mock import AsyncMock

import pytest
from gidgethub.aiohttp import GitHubAPI

from mtng.forge import GitHubForge


def client(token="first-token", host="https://api.github.com"):
    return GitHubAPI(None, "mtng-tests", oauth_token=token, base_url=host)


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
