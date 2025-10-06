import functools
from typing import Any, List, Optional, Literal, Dict
from datetime import datetime
import urllib.parse
import asyncio
import dateutil.parser

import pickle

from gidgethub.abc import GitHubAPI
import pydantic
import diskcache
import appdirs
from rich import print
from rich.rule import Rule
from rich.status import Status
from rich.progress import Progress, track

from mtng.spec import Repository


class Label(pydantic.BaseModel):
    name: str


class User(pydantic.BaseModel):
    login: str
    html_url: str


class Review(pydantic.BaseModel):
    user: User
    state: Literal["APPROVED", "COMMENTED", "CHANGES_REQUESTED", "DISMISSED"]
    body: str

    submitted_at: datetime


class IssueBase(pydantic.BaseModel):
    title: str
    user: User
    labels: List[Label]
    html_url: str
    number: int
    assignee: Optional[User]

    body: Optional[str]
    url: str

    updated_at: datetime
    created_at: datetime
    closed_at: Optional[datetime]

    is_wip: bool = False
    is_stale: bool = False

    draft: Optional[bool] = None


class Issue(IssueBase):
    pull_request: Optional[Any] = None

    @property
    def is_pr(self) -> bool:
        return self.pull_request is not None


class PullRequest(IssueBase):
    requested_reviewers: List[User] = pydantic.Field(default_factory=list)
    reviews: List[Review] = pydantic.Field(default_factory=list)

    @property
    def is_pr(self) -> bool:
        return True


cache = diskcache.Cache(appdirs.user_cache_dir("mtng"))


def memoize(expire=0, key_func=None):
    def decorator(fn):
        @functools.wraps(fn)
        async def wrapped(*args, **kwargs):
            if key_func is None:
                _args, _kwargs = args, kwargs
            else:
                _args, _kwargs = key_func(args, kwargs)
            key = (
                fn.__name__.encode("utf-8")
                + b"_"
                + pickle.dumps(_args)
                + b"_"
                + pickle.dumps(_kwargs)
            )

            if hit := cache.get(key):
                return hit

            result = await fn(*args, **kwargs)
            cache.set(key, result, expire=expire)
            return result

        return wrapped

    return decorator


def strip_github_api(args, kwargs):
    kwargs.pop("gh", None)
    args = list(filter(lambda o: not isinstance(o, GitHubAPI), args))
    return args, kwargs


@memoize(expire=300, key_func=strip_github_api)
async def getitem(gh: GitHubAPI, url: str, *args: Any, **kwargs: Any) -> Any:
    return await gh.getitem(url, *args, **kwargs)


#  @memoize(expire=300, key_func=strip_github_api)
async def get_merged_pulls(
    gh: GitHubAPI,
    repo_name: str,
    start: datetime,
    end: datetime,
    with_labels: List[str] = [],
    without_labels: List[str] = [],
) -> List[PullRequest]:
    url = f"/search/issues?q=repo:{repo_name}+is:pr+merged:{start:%Y-%m-%d}..{end:%Y-%m-%d}"
    for label in without_labels:
        url += f'+-label:"{urllib.parse.quote(label)}"'
    for label in with_labels:
        url += f'+label:"{urllib.parse.quote(label)}"'

    with Status("Getting merged PR list"):
        items = [Issue.parse_obj(issue) async for issue in gh.getiter(url)]

    prs = [
        PullRequest.parse_obj(await getitem(gh, item.pull_request["url"]))
        for item in track(items, description="Getting PR details")
    ]

    for pr in track(prs, description="Getting PR reviews"):
        pr.reviews = [
            Review.parse_obj(r) for r in await getitem(gh, f"{pr.url}/reviews")
        ]

    return prs


@memoize(expire=300, key_func=strip_github_api)
async def get_open_issues(
    gh: GitHubAPI,
    repo_name: str,
    with_labels: List[str] = [],
    without_labels: List[str] = [],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    type: Literal["pr", "issue", "any"] = "issue",
) -> List[Issue]:
    url = f"/search/issues?q=repo:{repo_name}+is:open"
    if type != "any":
        url += f"+is:{type}"
    if start is not None and end is not None:
        url += f"+created:{start:%Y-%m-%d}..{end:%Y-%m-%d}"
    elif start is not None:
        url += f"+created:{start:%Y-%m-%d}..*"
    elif end is not None:
        url += f"+created:*..{end:%Y-%m-%d}"
    for label in without_labels:
        url += f'+-label:"{urllib.parse.quote(label)}"'
    for label in with_labels:
        url += f'+label:"{urllib.parse.quote(label)}"'
    obj = [Issue.parse_obj(issue) async for issue in gh.getiter(url)]

    if type == "pr":
        obj

    return obj


@memoize(expire=300, key_func=strip_github_api)
async def get_open_pulls(
    gh: GitHubAPI,
    *args: Any,
    **kwargs: Any,
) -> List[PullRequest]:
    with Status("Getting open PR list"):
        items = await get_open_issues(gh, *args, type="pr", **kwargs)

    prs = [
        PullRequest.parse_obj(await getitem(gh, item.pull_request["url"]))
        for item in track(items, description="Getting PR details")
    ]

    for pr in track(prs, description="Getting PR reviews"):
        pr.reviews = [
            Review.parse_obj(r) for r in await getitem(gh, f"{pr.url}/reviews")
        ]

    return prs


async def collect_repositories(
    repos: List[Repository], since: datetime, now: datetime, gh: GitHubAPI
):
    data = {}

    for repo in repos:
        print(Rule(f"Collecting data for {repo.name}"))
        data[repo.name] = {}
        data[repo.name]["merged_prs"] = []
        data[repo.name]["open_prs"] = []
        data[repo.name]["stale"] = []
        data[repo.name]["recent_issues"] = []
        data[repo.name]["needs_discussion"] = []
        data[repo.name]["spec"] = repo

        if repo.do_merged_prs:
            print(Rule("Fetching merged PRs", align="left"))
            merged_prs = await get_merged_pulls(
                gh,
                repo.name,
                since,
                now,
                without_labels=repo.filter_labels,
            )
            data[repo.name]["merged_prs"] = merged_prs

        if repo.do_open_prs:
            print(Rule("Fetching open PRs", align="left"))
            open_prs = await get_open_pulls(
                gh,
                repo.name,
                without_labels=repo.filter_labels,
            )

            if not repo.show_wip:
                open_prs = list(
                    filter(
                        lambda pr: repo.wip_label not in [l.name for l in pr.labels],
                        open_prs,
                    )
                )
            data[repo.name]["open_prs"] = open_prs

        if repo.do_stale:
            if repo.stale_label is None:
                raise ValueError("Provide stale label if do_stale=True")
            with Status("Getting stale issues"):
                stale = await get_open_issues(
                    gh,
                    repo.name,
                    with_labels=[repo.stale_label],
                    without_labels=repo.filter_labels,
                    type="any",
                )

                data[repo.name]["stale"] = stale

        if repo.do_recent_issues:
            with Status("Getting recent issues"):
                recent_issues = await get_open_issues(
                    gh,
                    repo.name,
                    start=since,
                    end=now,
                    without_labels=repo.filter_labels,
                )

                data[repo.name]["recent_issues"] = recent_issues

        for prk in "open_prs", "merged_prs", "stale", "recent_issues":
            for pr in data[repo.name][prk]:
                pr.is_wip = repo.wip_label in [l.name for l in pr.labels]
                if pr.is_pr:
                    pr.is_wip = pr.is_wip or (
                        pr.draft if pr.draft is not None else False
                    )
                pr.is_stale = repo.stale_label in [l.name for l in pr.labels]

        if repo.needs_discussion_label is not None:
            with Status("Getting items that need discussion"):
                needs_discussion = await get_open_issues(
                    gh,
                    repo.name,
                    with_labels=[repo.needs_discussion_label],
                    without_labels=repo.filter_labels,
                )
                data[repo.name]["needs_discussion"] = needs_discussion

    return data
