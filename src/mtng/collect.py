import functools
from typing import Any, List, Optional, Literal, Dict
from datetime import datetime
import urllib.parse
import asyncio
import dateutil.parser
import re

import pickle

from gidgethub.abc import GitHubAPI
import pydantic
import diskcache
import appdirs
from rich import print
from rich.rule import Rule
from rich.status import Status

from mtng.spec import Repository


class Label(pydantic.BaseModel):
    name: str


class User(pydantic.BaseModel):
    login: str
    html_url: str


class Review(pydantic.BaseModel):
    user: User
    state: Literal["APPROVED", "COMMENTED", "CHANGES_REQUESTED", "DISMISSED", "PENDING"]
    body: Optional[str] = None

    submitted_at: Optional[datetime] = None


class IssueBase(pydantic.BaseModel):
    title: str
    user: User
    labels: List[Label]
    html_url: str
    number: int
    assignee: Optional[User] = None

    body: Optional[str] = None
    url: str

    updated_at: datetime
    created_at: datetime
    closed_at: Optional[datetime] = None

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

_CACHE_MISS = object()
FETCH_CONCURRENCY = 8


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

            hit = cache.get(key, default=_CACHE_MISS)
            if hit is not _CACHE_MISS:
                return hit

            result = await fn(*args, **kwargs)
            cache.set(key, result, expire=expire)
            return result

        return wrapped

    return decorator


async def bounded_gather(items, fn, concurrency: int = FETCH_CONCURRENCY):
    sem = asyncio.Semaphore(concurrency)

    async def wrapped(item):
        async with sem:
            return await fn(item)

    return await asyncio.gather(*(wrapped(item) for item in items))


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
    url = f"/search/issues?q=repo:{repo_name}+is:pull-request+merged:{start:%Y-%m-%d}..{end:%Y-%m-%d}"
    for label in without_labels:
        url += f'+-label:"{urllib.parse.quote(label)}"'
    for label in with_labels:
        url += f'+label:"{urllib.parse.quote(label)}"'

    with Status("Getting merged PR list"):
        items = [Issue.model_validate(issue) async for issue in gh.getiter(url)]

    with Status("Getting PR details"):
        prs = await bounded_gather(
            items,
            lambda item: getitem(gh, item.pull_request["url"]),
        )
    prs = [PullRequest.model_validate(pr) for pr in prs]

    async def enrich_reviews(pr: PullRequest) -> PullRequest:
        pr.reviews = [
            Review.model_validate(r) for r in await getitem(gh, f"{pr.url}/reviews")
        ]
        return pr

    with Status("Getting PR reviews"):
        prs = await bounded_gather(prs, enrich_reviews)

    return prs


def extract_release_pull_numbers(
    release_body: str, repo_name: str, max_results: Optional[int] = None
) -> List[int]:
    url_pattern = re.compile(
        rf"https://github\.com/{re.escape(repo_name)}/pull/(\d+)\b", re.IGNORECASE
    )
    generated_notes_pattern = re.compile(r"\(#(\d+)\)")

    numbers = []
    seen = set()

    def add_number(number: int) -> bool:
        if number in seen:
            return False
        seen.add(number)
        numbers.append(number)
        return max_results is not None and len(numbers) >= max_results

    for m in url_pattern.finditer(release_body):
        if add_number(int(m.group(1))):
            return numbers

    for m in generated_notes_pattern.finditer(release_body):
        if add_number(int(m.group(1))):
            return numbers

    return numbers


def parse_release_tag(release_ref: str, repo_name: str) -> str:
    release_ref = release_ref.strip()
    release_url = re.match(
        r"^https://github\.com/(?P<owner_repo>[^/]+/[^/]+)/releases/tag/(?P<tag>[^/?#]+)",
        release_ref,
    )
    if release_url is None:
        return release_ref

    owner_repo = release_url.group("owner_repo")
    if owner_repo.lower() != repo_name.lower():
        raise ValueError(
            f"Release URL repository '{owner_repo}' does not match configured repository '{repo_name}'."
        )
    return urllib.parse.unquote(release_url.group("tag"))


@memoize(expire=300, key_func=strip_github_api)
async def get_release_pulls(
    gh: GitHubAPI,
    repo_name: str,
    release_ref: str,
    with_labels: List[str] = [],
    without_labels: List[str] = [],
) -> tuple[str, List[PullRequest]]:
    release_tag = parse_release_tag(release_ref, repo_name)
    release = await getitem(
        gh,
        f"/repos/{repo_name}/releases/tags/{urllib.parse.quote(release_tag, safe='')}",
    )
    release_body = release.get("body") or ""
    pr_numbers = extract_release_pull_numbers(release_body, repo_name)
    if len(pr_numbers) == 0:
        raise ValueError(
            f"No PR links for '{repo_name}' were found in release '{release_tag}' description."
        )

    with Status(f"Fetching PRs from release {release_tag}"):
        prs = await bounded_gather(
            pr_numbers,
            lambda pr_number: getitem(gh, f"/repos/{repo_name}/pulls/{pr_number}"),
        )
    prs = [PullRequest.model_validate(pr) for pr in prs]
    prs = [
        pr
        for pr in prs
        if not with_labels
        or all(label in [l.name for l in pr.labels] for label in with_labels)
    ]
    prs = [
        pr
        for pr in prs
        if not without_labels
        or all(label not in [l.name for l in pr.labels] for label in without_labels)
    ]

    async def enrich_reviews(pr: PullRequest) -> PullRequest:
        pr.reviews = [
            Review.model_validate(r) for r in await getitem(gh, f"{pr.url}/reviews")
        ]
        return pr

    with Status("Getting PR reviews"):
        prs = await bounded_gather(prs, enrich_reviews)

    return release_tag, prs


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
    def build_url(kind: Literal["issue", "pr"]) -> str:
        mapped_kind = "pull-request" if kind == "pr" else "issue"
        url = f"/search/issues?q=repo:{repo_name}+is:open+is:{mapped_kind}"
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
        return url

    if type in ("issue", "pr"):
        return [
            Issue.model_validate(issue) async for issue in gh.getiter(build_url(type))
        ]

    issues = [
        Issue.model_validate(issue) async for issue in gh.getiter(build_url("issue"))
    ]
    prs = [Issue.model_validate(issue) async for issue in gh.getiter(build_url("pr"))]
    # Keep deterministic ordering and prevent accidental duplicates.
    merged: Dict[str, Issue] = {issue.url: issue for issue in issues}
    for pr in prs:
        merged.setdefault(pr.url, pr)
    return list(merged.values())


@memoize(expire=300, key_func=strip_github_api)
async def get_open_pulls(
    gh: GitHubAPI,
    *args: Any,
    **kwargs: Any,
) -> List[PullRequest]:
    with Status("Getting open PR list"):
        items = await get_open_issues(gh, *args, type="pr", **kwargs)

    with Status("Getting PR details"):
        prs = await bounded_gather(
            items,
            lambda item: getitem(gh, item.pull_request["url"]),
        )
    prs = [PullRequest.model_validate(pr) for pr in prs]

    async def enrich_reviews(pr: PullRequest) -> PullRequest:
        pr.reviews = [
            Review.model_validate(r) for r in await getitem(gh, f"{pr.url}/reviews")
        ]
        return pr

    with Status("Getting PR reviews"):
        prs = await bounded_gather(prs, enrich_reviews)

    return prs


async def collect_repositories(
    repos: List[Repository],
    since: datetime,
    now: datetime,
    gh: GitHubAPI,
    release: Optional[str] = None,
):
    data = {}

    for repo in repos:
        print(Rule(f"Collecting data for {repo.name}"))
        key = repo.display_name or repo.name
        data[key] = {}
        data[key]["merged_prs"] = []
        data[key]["open_prs"] = []
        data[key]["stale"] = []
        data[key]["recent_issues"] = []
        data[key]["needs_discussion"] = []
        data[key]["release_tag"] = None
        data[key]["spec"] = repo

        if repo.do_merged_prs:
            print(Rule("Fetching merged PRs", align="left"))
            if release is not None:
                release_tag, merged_prs = await get_release_pulls(
                    gh,
                    repo.name,
                    release,
                    with_labels=repo.with_labels,
                    without_labels=repo.without_labels,
                )
                data[key]["release_tag"] = release_tag
            else:
                merged_prs = await get_merged_pulls(
                    gh,
                    repo.name,
                    since,
                    now,
                    with_labels=repo.with_labels,
                    without_labels=repo.without_labels,
                )
            data[key]["merged_prs"] = merged_prs

        if release is not None:
            # In release mode we intentionally only show PRs that landed in the
            # selected release and skip open/stale/recent/discussion lists.
            continue

        if repo.do_open_prs:
            print(Rule("Fetching open PRs", align="left"))
            open_prs = await get_open_pulls(
                gh,
                repo.name,
                with_labels=repo.with_labels,
                without_labels=repo.without_labels,
            )

            if not repo.show_wip:
                open_prs = list(
                    filter(
                        lambda pr: repo.wip_label not in [l.name for l in pr.labels],
                        open_prs,
                    )
                )
            data[key]["open_prs"] = open_prs

        if repo.do_stale:
            if repo.stale_label is None:
                raise ValueError("Provide stale label if do_stale=True")
            with Status("Getting stale issues"):
                stale = await get_open_issues(
                    gh,
                    repo.name,
                    with_labels=[repo.stale_label] + repo.with_labels,
                    without_labels=repo.without_labels,
                    type="any",
                )

                data[key]["stale"] = stale

        if repo.do_recent_issues:
            with Status("Getting recent issues"):
                recent_issues = await get_open_issues(
                    gh,
                    repo.name,
                    start=since,
                    end=now,
                    with_labels=repo.with_labels,
                    without_labels=repo.without_labels,
                )

                data[key]["recent_issues"] = recent_issues

        for prk in "open_prs", "merged_prs", "stale", "recent_issues":
            for pr in data[key][prk]:
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
                    with_labels=[repo.needs_discussion_label] + repo.with_labels,
                    without_labels=repo.without_labels,
                )
                data[key]["needs_discussion"] = needs_discussion

    return data
