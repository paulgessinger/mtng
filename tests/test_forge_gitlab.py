"""Synthetic GitLab REST fixtures exercise the adapter without credentials."""

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import aiohttp
import pytest

from mtng.forge.gitlab import (
    GitLabError,
    GitLabForge,
    extract_release_numbers,
    parse_release_tag,
)
from mtng.spec import Repository, Spec
from mtng.collect import collect_repositories
from mtng.generate import generate_latex
from fake_forge import FakeForge

START = datetime(2026, 1, 1, tzinfo=timezone.utc)
END = datetime(2026, 1, 31, tzinfo=timezone.utc)
REPO = "group/sub/project"
USER = {
    "username": "alice",
    "web_url": "https://gitlab.example/alice",
    "state": "active",
}


def payload(iid=1, **kwargs):
    return dict(
        {
            "id": 1000 + iid,
            "iid": iid,
            "title": "feat: new thing",
            "author": USER,
            "web_url": f"https://gitlab.example/{REPO}/-/merge_requests/{iid}",
            "description": None,
            "labels": ["feature"],
            "assignees": [],
            "reviewers": [USER],
            "created_at": "2026-01-02T00:00:00Z",
            "updated_at": "2026-02-20T00:00:00Z",
            "merged_at": "2026-01-10T00:00:00Z",
            "closed_at": None,
            "state": "merged",
        },
        **kwargs,
    )


@pytest.fixture
def forge():
    return GitLabForge(None, "gitlab.example", "test-token")


def test_mapping(forge):
    mr = forge.item(payload(draft=True), REPO, mr=True)
    assert mr.number == 1 and mr.is_pr and mr.draft
    assert mr.assignee is None and mr.body is None
    assert mr.closed_at == mr.merged_at
    assert mr.additions is None and not mr.reviews_available
    assert mr.requested_reviewers[0].login == "alice"
    assert "group%2Fsub%2Fproject/merge_requests/1" in mr.url
    issue = forge.item(payload(merged_at=None), REPO, mr=False)
    assert not issue.is_pr and issue.url != mr.url


@pytest.mark.asyncio
async def test_merge_window_does_not_exclude_later_updates(forge):
    forge.request = AsyncMock(
        side_effect=[
            (
                [payload(), payload(2, merged_at="2025-12-01T00:00:00Z")],
                {"X-Next-Page": "2"},
            ),
            ([payload(3, merged_at="2026-02-01T00:00:00Z")], {"X-Next-Page": ""}),
            ({"approved_by": [{"user": USER}]}, {}),
        ]
    )
    prs = await forge.merged_prs(REPO, START, END)
    assert [pr.number for pr in prs] == [1]
    assert prs[0].reviews[0].state == "APPROVED"
    params = forge.request.call_args_list[0].args[1]
    assert (
        "updated_before" not in params and params["updated_after"] == START.isoformat()
    )
    assert params["page"] == 1
    assert forge.request.call_args_list[1].args[1]["page"] == 2
    assert forge.request.await_count == 3


@pytest.mark.asyncio
async def test_issues_include_mrs_without_colliding_iids(forge):
    forge.request = AsyncMock(
        side_effect=[
            ([payload(merged_at=None)], {"X-Next-Page": ""}),
            ([payload(merged_at=None)], {"X-Next-Page": ""}),
        ]
    )
    items = await forge.open_issues(REPO, include_prs=True, start=START, end=END)
    assert [item.is_pr for item in items] == [False, True]
    assert len({item.url for item in items}) == 2


@pytest.mark.asyncio
async def test_labels_are_exact_and_all_required(forge):
    forge.request = AsyncMock(
        return_value=(
            [
                payload(1, labels=["a,b", "None"]),
                payload(2, labels=["a", "b"]),
                payload(3, labels=["a,b", "None", "excluded"]),
            ],
            {"X-Next-Page": ""},
        )
    )
    items = await forge.open_issues(
        REPO, with_labels=["a,b", "None"], without_labels=["excluded"]
    )
    assert [item.number for item in items] == [1]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 405, 500])
async def test_approvals_only_tolerate_missing_capability(forge, status):
    forge.getitem = AsyncMock(side_effect=GitLabError(status, "/approvals"))
    pr = forge.item(payload(), REPO, mr=True)
    if status in (404, 405):
        result = await forge.add_reviews(REPO, [pr])
        assert not result[0].reviews_available
    else:
        with pytest.raises(GitLabError):
            await forge.add_reviews(REPO, [pr])


def test_release_refs():
    assert (
        parse_release_tag(
            f"https://gitlab.example/{REPO}/-/releases/v1%2Frc", REPO, "gitlab.example"
        )
        == "v1/rc"
    )
    for ref in [
        f"https://evil.example/{REPO}/-/releases/v1",
        "https://gitlab.example/other/-/releases/v1",
    ]:
        with pytest.raises(ValueError):
            parse_release_tag(ref, REPO, "gitlab.example")
    body = f"!1 !1 https://gitlab.example/{REPO}/-/merge_requests/2 other/project!3 https://other.example/x/!4"
    assert extract_release_numbers(body, REPO, "gitlab.example") == [2, 1]


@pytest.mark.asyncio
async def test_release_filters_nonmerged_and_labels(forge):
    forge.getitem = AsyncMock(
        side_effect=[
            {"description": "!1 !2 !3"},
            payload(),
            payload(2, state="opened", merged_at=None),
            payload(3, labels=["excluded"]),
            {"approved_by": []},
        ]
    )
    tag, prs = await forge.release_prs(REPO, "v1/rc", without_labels=["excluded"])
    assert tag == "v1/rc" and [pr.number for pr in prs] == [1]
    assert forge.getitem.call_args_list[0].args[0].endswith("/releases/v1%2Frc")


class Response:
    def __init__(self, status, data=None, headers=None):
        self.status, self.data, self.headers = status, data, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def read(self):
        import json

        return json.dumps(self.data).encode()


class Session:
    def __init__(self, responses):
        self.responses, self.calls = iter(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((url, kwargs))
        return next(self.responses)


@pytest.mark.asyncio
async def test_transport_preserves_encoded_paths_and_refuses_redirects():
    session = Session([Response(302, headers={"Location": "https://evil.example"})])
    forge = GitLabForge(session, "gitlab.example", "secret")
    with pytest.raises(GitLabError):
        await forge.getitem("/projects/group%2Fsub%2Fproject/releases/v1%2Frc")
    url, options = session.calls[0]
    assert "%2F" in str(url) and options["allow_redirects"] is False
    assert options["headers"] == {"PRIVATE-TOKEN": "secret"}
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_transport_retries_rate_limit(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("mtng.forge.http.asyncio.sleep", sleep)
    session = Session(
        [Response(429, headers={"Retry-After": "1"}), Response(200, {"ok": True})]
    )
    assert await GitLabForge(session, "gitlab.example", "secret").getitem("/user") == {
        "ok": True
    }
    assert sleep.await_count == 1
    assert sleep.call_args.args[0] == pytest.approx(1, abs=0.1)


def test_cache_identity():
    identities = {
        GitLabForge(None, host, token).cache_id
        for host in ["gitlab.com", "gitlab.cern.ch"]
        for token in ["one", "two"]
    }
    assert len(identities) == 4
    assert all(not value.endswith(":one") for value in identities)


@pytest.mark.asyncio
async def test_mixed_forges_render_separate_report_sections(forge):
    github = Repository(name="group/project", display_name="GitHub")
    gitlab = Repository(
        name="group/project",
        display_name="GitLab",
        forge="gitlab",
        host="gitlab.example",
        do_reviewers=True,
    )
    mr = forge.item(payload(), "group/project", mr=True)
    spec = Spec(repos=[github, gitlab])
    data = await collect_repositories(
        spec.repos,
        START,
        END,
        {
            github.forge_key: FakeForge(merged_prs=[mr.model_copy(deep=True)]),
            gitlab.forge_key: FakeForge(merged_prs=[mr]),
        },
    )
    assert list(data) == ["GitHub", "GitLab"]
    output = generate_latex(spec, data, START, END, [], False)
    assert "!1" in output and "Merged MRs" in output and "Merged PRs" in output
    assert "review data unavailable" in output
    assert data["GitLab"]["release_stats"].reviewers is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_live_gitlab():
    token, repo = os.environ.get("GITLAB_TOKEN"), os.environ.get(
        "MTNG_GITLAB_TEST_PROJECT"
    )
    if not token or not repo:
        pytest.skip(
            "Set GITLAB_TOKEN and MTNG_GITLAB_TEST_PROJECT for live GitLab tests"
        )
    host = os.environ.get("MTNG_GITLAB_TEST_HOST", "gitlab.com")
    async with aiohttp.ClientSession() as session:
        forge = GitLabForge(session, host, token)
        data, _ = await forge.request("/user")
        assert data["username"]
        await forge.open_issues(repo)


@pytest.mark.asyncio
async def test_gitlab_snapshot(forge):
    from pathlib import Path

    repo = Repository(
        name=REPO, forge="gitlab", host="gitlab.example", do_reviewers=True
    )
    mr = forge.item(payload(), REPO, mr=True)
    mr.reviews_available = True
    data = await collect_repositories([repo], START, END, FakeForge(merged_prs=[mr]))
    output = generate_latex(Spec(repos=[repo]), data, START, END, [], False) + "\n"
    reference = Path(__file__).parent / "ref" / "gitlab" / "report.tex"
    if os.environ.get("UPDATE_SNAPSHOTS"):
        reference.parent.mkdir(parents=True, exist_ok=True)
        reference.write_text(output)
    assert output == reference.read_text()
    full = generate_latex(Spec(repos=[repo]), data, START, END, [], True)
    assert "Merge request and issue summary" in full
    assert "!1" in full


@pytest.mark.asyncio
async def test_pagination_rejects_repeated_page(forge):
    forge.request = AsyncMock(return_value=([payload()], {"X-Next-Page": "1"}))
    with pytest.raises(ValueError, match="non-advancing"):
        await forge.open_issues(REPO)


@pytest.mark.asyncio
async def test_transport_does_not_retry_authentication_errors():
    session = Session([Response(401)])
    with pytest.raises(GitLabError, match="401"):
        await GitLabForge(session, "gitlab.example", "secret").getitem("/user")
    assert len(session.calls) == 1


@pytest.mark.asyncio
async def test_transport_retries_are_bounded(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr("mtng.forge.http.asyncio.sleep", sleep)
    session = Session([Response(503), Response(503), Response(503)])
    with pytest.raises(GitLabError, match="503"):
        await GitLabForge(session, "gitlab.example", "secret").getitem("/user")
    assert len(session.calls) == 3 and sleep.await_count == 2
