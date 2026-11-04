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
    raw["assignees"] = [
        {"login": login, "html_url": f"https://github.com/{login}"}
        for login in ["alice", "bob"]
    ]
    raw["head"] = {"sha": "current-head"}
    raw["reviews"][0]["commit_id"] = "reviewed-head"
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
    assert [u.login for u in prs[0].assignees] == ["alice", "bob"]
    assert prs[0].head_sha == "current-head"
    assert prs[0].reviews[0].commit_id == "reviewed-head"
    assert gh._make_request.await_count == 3


@pytest.mark.asyncio
async def test_pull_diff_requests_diff_media_type():
    gh = client()
    gh.getitem = AsyncMock(return_value="diff --git a/file b/file")
    assert (
        await GitHubForge(gh).pull_diff("team/project", 42)
        == "diff --git a/file b/file"
    )
    gh.getitem.assert_awaited_once_with(
        "/repos/team/project/pulls/42", accept="application/vnd.github.diff"
    )


@pytest.mark.asyncio
async def test_pull_diff_survives_oversized_diff_rejection():
    """GitHub 400s on huge diffs; fall back to the per-file endpoint."""

    from gidgethub import BadRequest

    gh = client()
    gh.getitem = AsyncMock(
        side_effect=BadRequest(
            400, "Sorry, the diff exceeded the maximum number of lines (20000)"
        )
    )

    async def files(url):
        assert url == "/repos/team/project/pulls/42/files?per_page=100"
        yield {
            "filename": "core/big.py",
            "additions": 1200,
            "deletions": 300,
            "patch": "@@ -1,2 +1,3 @@\n+new",
        }
        yield {"filename": "docs/moved.md", "previous_filename": "docs/old.md"}
        yield {"filename": "data/blob.bin", "additions": 0, "deletions": 0}

    gh.getiter = files
    diff = await GitHubForge(gh).pull_diff("team/project", 42)
    assert diff.startswith("[Diff too large to fetch; 3 changed files:]")
    assert "core/big.py (+1200 -300)" in diff
    assert "docs/moved.md (+0 -0) (renamed from docs/old.md)" in diff
    assert "data/blob.bin (+0 -0)" in diff
    assert "+new" in diff  # patch excerpt included


@pytest.mark.asyncio
async def test_pull_diff_file_fallback_parks_patch_budget():
    from gidgethub import BadRequest

    gh = client()
    gh.getitem = AsyncMock(
        side_effect=BadRequest(
            400, "Sorry, the diff exceeded the maximum number of lines (20000)"
        )
    )

    async def files(url):
        yield {"filename": "a.py", "patch": "x" * 55000}
        yield {"filename": "b.py", "patch": "y" * 10000}

    gh.getiter = files
    diff = await GitHubForge(gh).pull_diff("team/project", 42)
    # First patch consumes most of the budget; the second is cut to the rest.
    assert ("x" * 55000) in diff
    assert ("y" * 5000 + "\n[Truncated]") in diff


@pytest.mark.asyncio
async def test_pull_diff_reraises_other_bad_requests():
    from gidgethub import BadRequest

    gh = client()
    gh.getitem = AsyncMock(side_effect=BadRequest(404, {"message": "Not Found"}))
    with pytest.raises(BadRequest):
        await GitHubForge(gh).pull_diff("team/project", 42)
