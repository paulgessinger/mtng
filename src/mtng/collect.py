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

from mtng.spec import Repository


class Label(pydantic.BaseModel):
    name: str


class User(pydantic.BaseModel):
    login: str
    html_url: str


class Issue(pydantic.BaseModel):
    title: str
    user: User
    labels: List[Label]
    html_url: str
    number: int
    assignee: Optional[User]

    updated_at: datetime
    created_at: datetime
    closed_at: Optional[datetime]

    is_wip: bool = False
    is_stale: bool = False

    pull_request: Optional[Any] = None

    @property
    def is_pr(self) -> bool:
        return self.pull_request is not None


class PullRequest(Issue):
    pass


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

    return [PullRequest.parse_obj(issue) async for issue in gh.getiter(url)]


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
    return [Issue.parse_obj(issue) async for issue in gh.getiter(url)]


async def collect_repositories(
    repos: List[Repository], since: datetime, now: datetime, gh: GitHubAPI
):
    data = {}
    for repo in repos:
        data[repo.name] = {}
        data[repo.name]["merged_prs"] = []
        data[repo.name]["open_prs"] = []
        data[repo.name]["stale"] = []
        data[repo.name]["recent_issues"] = []
        data[repo.name]["spec"] = repo

        if repo.do_merged_prs:
            merged_prs = await get_merged_pulls(
                gh,
                repo.name,
                since,
                now,
                without_labels=repo.filter_labels,
            )
            data[repo.name]["merged_prs"] = merged_prs

        if repo.do_open_prs:
            open_prs = await get_open_issues(
                gh,
                repo.name,
                without_labels=repo.filter_labels,
                type="pr",
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
            stale = await get_open_issues(
                gh,
                repo.name,
                with_labels=[repo.stale_label],
                without_labels=repo.filter_labels,
                type="any",
            )

            data[repo.name]["stale"] = stale

        if repo.do_recent_issues:
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
                pr.is_stale = repo.stale_label in [l.name for l in pr.labels]

    return data
