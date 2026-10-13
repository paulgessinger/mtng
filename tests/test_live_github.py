"""Small opt-in API contract check; broad collection uses offline fixtures."""

import os

import aiohttp
from gidgethub.aiohttp import GitHubAPI
import pytest

from mtng.models import PullRequest, Review


@pytest.mark.integration
@pytest.mark.asyncio
async def test_github_contract():
    token = os.environ.get("GH_TOKEN")
    repo = os.environ.get("MTNG_GITHUB_TEST_PROJECT")
    if not token or not repo:
        pytest.skip(
            "Set GH_TOKEN and MTNG_GITHUB_TEST_PROJECT for the GitHub smoke test"
        )
    # At most three requests, no search or unbounded pagination. Bypass the
    # application cache so a green smoke test really contacted the service.
    async with aiohttp.ClientSession() as session:
        gh = GitHubAPI(session, "mtng-smoke", oauth_token=token)
        metadata = await gh.getitem(f"/repos/{repo}")
        assert metadata["full_name"].lower() == repo.lower()
        prs = await gh.getitem(f"/repos/{repo}/pulls?state=all&per_page=1")
        if prs:
            pr = PullRequest.model_validate(prs[0])
            reviews = await gh.getitem(
                f"/repos/{repo}/pulls/{pr.number}/reviews?per_page=1"
            )
            for review in reviews:
                Review.model_validate(review)
