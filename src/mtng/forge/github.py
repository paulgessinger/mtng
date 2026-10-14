from typing import Any, Literal
from datetime import datetime
import urllib.parse
import re
import hashlib

from gidgethub.abc import GitHubAPI
from rich.status import Status

from mtng.forge.base import bounded_gather
from mtng.models import Issue, PullRequest, Review


def extract_release_pull_numbers(
    release_body: str, repo_name: str, max_results: int | None = None
) -> list[int]:
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


def label_query(with_labels: list[str], without_labels: list[str]) -> str:
    query = ""
    for label in without_labels:
        query += f'+-label:"{urllib.parse.quote(label)}"'
    for label in with_labels:
        query += f'+label:"{urllib.parse.quote(label)}"'
    return query


class GitHubForge:
    def __init__(self, gh: GitHubAPI):
        self.gh = gh

    @property
    def cache_id(self) -> str:
        # Visibility depends on both the API host and credentials. Never put
        # the token itself in a persisted cache key, and handle token rotation.
        credential = hashlib.sha256((self.gh.oauth_token or "").encode()).hexdigest()
        return f"github:{self.gh.base_url}:{credential}"

    async def getitem(self, url: str) -> Any:
        return await self.gh.getitem(url)

    async def get_reviews(self, url: str) -> list[Review]:
        return [Review.model_validate(r) async for r in self.gh.getiter(url)]

    async def pull_details(self, urls: list[str]) -> list[PullRequest]:
        """Fetch full PR payloads. Search results lack the line and commit
        counts, and the reviews are a separate request on top."""
        with Status("Getting PR details"):
            prs = await bounded_gather(urls, self.getitem)
        return [PullRequest.model_validate(pr) for pr in prs]

    async def add_reviews(self, prs: list[PullRequest]) -> list[PullRequest]:
        async def enrich_reviews(pr: PullRequest) -> PullRequest:
            pr.reviews = await self.get_reviews(f"{pr.url}/reviews")
            return pr

        with Status("Getting PR reviews"):
            return await bounded_gather(prs, enrich_reviews)

    async def merged_prs(
        self,
        repo_name: str,
        start: datetime,
        end: datetime,
        with_labels: list[str] = [],
        without_labels: list[str] = [],
    ) -> list[PullRequest]:
        url = f"/search/issues?q=repo:{repo_name}+is:pull-request+merged:{start:%Y-%m-%d}..{end:%Y-%m-%d}"
        url += label_query(with_labels, without_labels)

        with Status("Getting merged PR list"):
            items = [
                Issue.model_validate(issue) async for issue in self.gh.getiter(url)
            ]

        prs = await self.pull_details([item.pull_request["url"] for item in items])
        return await self.add_reviews(prs)

    async def release_prs(
        self,
        repo_name: str,
        release_ref: str,
        with_labels: list[str] = [],
        without_labels: list[str] = [],
    ) -> tuple[str, list[PullRequest]]:
        release_tag = parse_release_tag(release_ref, repo_name)
        release = await self.getitem(
            f"/repos/{repo_name}/releases/tags/{urllib.parse.quote(release_tag, safe='')}",
        )
        release_body = release.get("body") or ""
        pr_numbers = extract_release_pull_numbers(release_body, repo_name)
        if len(pr_numbers) == 0:
            raise ValueError(
                f"No PR links for '{repo_name}' were found in release '{release_tag}' description."
            )

        with Status(f"Fetching PRs from release {release_tag}"):
            prs = await self.pull_details(
                [f"/repos/{repo_name}/pulls/{n}" for n in pr_numbers]
            )
        # PRs come from the release body rather than a search, so the label
        # filters have to be applied here.
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

        return release_tag, await self.add_reviews(prs)

    async def open_prs(
        self,
        repo_name: str,
        with_labels: list[str] = [],
        without_labels: list[str] = [],
    ) -> list[PullRequest]:
        with Status("Getting open PR list"):
            items = await self.search_open(repo_name, "pr", with_labels, without_labels)

        prs = await self.pull_details([item.pull_request["url"] for item in items])
        return await self.add_reviews(prs)

    async def open_issues(
        self,
        repo_name: str,
        with_labels: list[str] = [],
        without_labels: list[str] = [],
        start: datetime | None = None,
        end: datetime | None = None,
        include_prs: bool = False,
    ) -> list[Issue]:
        issues = await self.search_open(
            repo_name, "issue", with_labels, without_labels, start, end
        )
        if not include_prs:
            return issues

        prs = await self.search_open(
            repo_name, "pr", with_labels, without_labels, start, end
        )
        # Keep deterministic ordering and prevent accidental duplicates.
        merged: dict[str, Issue] = {issue.url: issue for issue in issues}
        for pr in prs:
            merged.setdefault(pr.url, pr)
        return list(merged.values())

    async def search_open(
        self,
        repo_name: str,
        kind: Literal["issue", "pr"],
        with_labels: list[str] = [],
        without_labels: list[str] = [],
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[Issue]:
        mapped_kind = "pull-request" if kind == "pr" else "issue"
        url = f"/search/issues?q=repo:{repo_name}+is:open+is:{mapped_kind}"
        if start is not None and end is not None:
            url += f"+created:{start:%Y-%m-%d}..{end:%Y-%m-%d}"
        elif start is not None:
            url += f"+created:{start:%Y-%m-%d}..*"
        elif end is not None:
            url += f"+created:*..{end:%Y-%m-%d}"
        url += label_query(with_labels, without_labels)
        return [Issue.model_validate(issue) async for issue in self.gh.getiter(url)]
