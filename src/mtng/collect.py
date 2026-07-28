import functools
from typing import Any, Dict, List, Literal, Optional
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

from mtng.spec import (
    DEFAULT_PR_CATEGORY_COLORS,
    DEFAULT_PR_CATEGORY_EMOJI,
    DEFAULT_PR_CATEGORY_LABELS,
    DEFAULT_PR_CATEGORY_ORDER,
    Repository,
)
from mtng.stats import compute_release_stats


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
    pr_title_display: Optional[str] = None
    pr_category: Optional[str] = None
    pr_category_label: Optional[str] = None
    pr_category_breaking: bool = False
    pr_category_color: Optional[str] = None
    pr_category_order: int = 999

    draft: Optional[bool] = None


class Issue(IssueBase):
    pull_request: Optional[Any] = None

    @property
    def is_pr(self) -> bool:
        return self.pull_request is not None


class PullRequest(IssueBase):
    requested_reviewers: List[User] = pydantic.Field(default_factory=list)
    reviews: List[Review] = pydantic.Field(default_factory=list)

    # Only populated by GET /repos/{repo}/pulls/{n}. The /search/issues endpoint
    # never returns these, so anything sourced from a search stays None.
    additions: Optional[int] = None
    deletions: Optional[int] = None
    changed_files: Optional[int] = None
    commits: Optional[int] = None
    merged_at: Optional[datetime] = None

    @property
    def is_pr(self) -> bool:
        return True


cache = diskcache.Cache(appdirs.user_cache_dir("mtng"))

_CACHE_MISS = object()
FETCH_CONCURRENCY = 8

# Bump whenever a model stored in the cache gains or loses fields. Entries are
# pickled pydantic instances, so an old entry restored into a new class is
# missing the new attributes entirely and raises AttributeError on access.
CACHE_VERSION = 3

CONVENTIONAL_TITLE_RE = re.compile(
    r"^(?P<kind>[a-z][a-z0-9-]*)(?:\([^)]+\))?(?P<breaking>!)?:\s*(?P<subject>.+)$",
    re.IGNORECASE,
)
CATEGORY_ORDER = tuple(DEFAULT_PR_CATEGORY_ORDER)
CATEGORY_ORDER_INDEX = {name: index for index, name in enumerate(CATEGORY_ORDER)}


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
                + b"_v"
                + str(CACHE_VERSION).encode("utf-8")
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


def parse_pr_metadata(title: str) -> tuple[str, bool, str, bool]:
    m = CONVENTIONAL_TITLE_RE.match(title.strip())
    if m is None:
        return "other", False, title.strip(), False
    return (
        m.group("kind").lower(),
        bool(m.group("breaking")),
        m.group("subject").strip(),
        True,
    )


def parse_pr_category(title: str) -> tuple[str, bool]:
    category, is_breaking, _, _ = parse_pr_metadata(title)
    return category, is_breaking


def resolve_category_order(order: Optional[List[str]] = None) -> Dict[str, int]:
    """Position of every known category: the configured keys first, then any
    default category the configuration left out."""
    if order is None:
        return dict(CATEGORY_ORDER_INDEX)
    resolved = list(order)
    resolved += [name for name in DEFAULT_PR_CATEGORY_ORDER if name not in resolved]
    return {name: index for index, name in enumerate(resolved)}


def category_order(category: str, order: Optional[List[str]] = None) -> int:
    index = resolve_category_order(order)
    return index.get(category, len(index))


def resolve_category_color(
    category: str, is_breaking: bool, category_colors: Dict[str, str]
) -> str:
    if is_breaking:
        return category_colors.get(
            "breaking", DEFAULT_PR_CATEGORY_COLORS.get("breaking", "alertred")
        )
    return category_colors.get(
        category,
        DEFAULT_PR_CATEGORY_COLORS.get(
            category,
            category_colors.get(
                "other", DEFAULT_PR_CATEGORY_COLORS.get("other", "beige")
            ),
        ),
    )


def resolve_category_label(category: str, category_labels: Dict[str, str]) -> str:
    return category_labels.get(
        category,
        DEFAULT_PR_CATEGORY_LABELS.get(
            category,
            category_labels.get(
                "other",
                DEFAULT_PR_CATEGORY_LABELS.get(
                    "other", category.replace("-", " ").strip().capitalize()
                ),
            ),
        ),
    )


def enrich_item(item: IssueBase, repo: Repository) -> None:
    labels = [l.name for l in item.labels]
    item.is_wip = repo.wip_label in labels
    if item.is_pr:
        item.is_wip = item.is_wip or (item.draft if item.draft is not None else False)
    item.is_stale = repo.stale_label in labels

    if not item.is_pr:
        return

    category, is_breaking, display_title, parsed = parse_pr_metadata(item.title)
    item.pr_category = category
    item.pr_category_breaking = is_breaking
    item.pr_category_label = resolve_category_label(category, repo.pr_category_labels)
    item.pr_category_color = resolve_category_color(
        category, is_breaking, repo.pr_category_colors
    )
    item.pr_category_order = category_order(category, repo.pr_category_order)
    if parsed:
        item.pr_title_display = display_title


def group_prs_by_category(items: List[IssueBase]) -> List[Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if not item.is_pr:
            continue
        category = item.pr_category or "other"
        group = grouped.setdefault(
            category,
            {
                "category": category,
                "title": item.pr_category_label or category,
                "emoji": DEFAULT_PR_CATEGORY_EMOJI.get(category),
                # Set per repository by enrich_item, so the configured
                # pr_category_order carries over to the section sequence.
                "order": item.pr_category_order,
                "items": [],
            },
        )
        group["items"].append(item)
    return sorted(grouped.values(), key=lambda group: (group["order"], group["title"]))


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
        data[key]["merged_prs_listed"] = []
        data[key]["breaking_prs"] = []
        data[key]["open_prs"] = []
        data[key]["stale"] = []
        data[key]["recent_issues"] = []
        data[key]["needs_discussion"] = []
        data[key]["release_tag"] = None
        data[key]["release_stats"] = None
        data[key]["merged_prs_by_category"] = []
        data[key]["open_prs_by_category"] = []
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
            data[key]["release_stats"] = compute_release_stats(
                data[key]["merged_prs"],
                ignore_labels=[
                    label for label in (repo.wip_label, repo.stale_label) if label
                ],
            )
            for pr in data[key]["merged_prs"]:
                enrich_item(pr, repo)

            data[key]["breaking_prs"] = [
                pr for pr in merged_prs if pr.pr_category_breaking
            ]
            # Without repeat_breaking_changes, a breaking PR is only listed on
            # its own slide, not again under its category.
            listed = (
                merged_prs
                if repo.repeat_breaking_changes or not repo.show_breaking_changes
                else [pr for pr in merged_prs if not pr.pr_category_breaking]
            )
            data[key]["merged_prs_listed"] = listed
            data[key]["merged_prs_by_category"] = group_prs_by_category(listed)

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

        for item_type in "open_prs", "stale", "recent_issues":
            for item in data[key][item_type]:
                enrich_item(item, repo)
        data[key]["open_prs_by_category"] = group_prs_by_category(data[key]["open_prs"])

        if repo.needs_discussion_label is not None:
            with Status("Getting items that need discussion"):
                needs_discussion = await get_open_issues(
                    gh,
                    repo.name,
                    with_labels=[repo.needs_discussion_label] + repo.with_labels,
                    without_labels=repo.without_labels,
                )
                data[key]["needs_discussion"] = needs_discussion
                for item in data[key]["needs_discussion"]:
                    enrich_item(item, repo)

    return data
