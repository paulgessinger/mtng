"""GitLab REST adapter. Report layout and section selection live elsewhere."""

import hashlib
import re
from datetime import datetime
from collections.abc import Sequence
from urllib.parse import quote, unquote, urlsplit

import aiohttp
from yarl import URL

from mtng.forge.http import HttpTransport
from mtng.forge.base import bounded_gather, FetchRequirements
from mtng.models import Issue, Label, PullRequest, Review, User
from mtng.spec import normalize_host
from mtng.progress import collect_items


class GitLabError(ValueError):
    def __init__(self, status: int, path: str):
        self.status = status
        super().__init__(f"GitLab request failed ({status}): {path}")


def parse_release_tag(ref: str, repo: str, host: str) -> str:
    ref = ref.strip()
    if "://" not in ref:
        return ref
    parsed = urlsplit(ref)
    prefix = f"/{repo}/-/releases/"
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != host
        or not parsed.path.startswith(prefix)
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Release URL must match the configured GitLab host and project"
        )
    tag = unquote(parsed.path[len(prefix) :])
    if not tag:
        raise ValueError("Release URL must contain a tag")
    return tag


def extract_release_numbers(body: str, repo: str, host: str) -> list[int]:
    # Strip all URLs before scanning shorthand so foreign-project URLs cannot
    # accidentally contribute local MR numbers.
    urls = re.findall(r"https?://[^\s<>\"\)]+", body)
    prefix = f"https://{host}/{repo}/-/merge_requests/"
    numbers = []
    for url in urls:
        match = re.fullmatch(re.escape(prefix) + r"(\d+)(?:[?#].*)?", url)
        if match:
            numbers.append(int(match[1]))
    text = re.sub(r"https?://[^\s<>\"\)]+", "", body)
    numbers.extend(int(n) for n in re.findall(r"(?<![\w/!.-])!(\d+)\b", text))
    return list(dict.fromkeys(numbers))


class GitLabForge:
    def __init__(
        self, session: aiohttp.ClientSession, host: str, token: str, *, transport=None
    ):
        self.session = session
        self.transport = transport or HttpTransport(session)
        self.host = normalize_host(host)
        self.token = token
        self.base_url = f"https://{self.host}/api/v4"

    @property
    def cache_id(self) -> str:
        digest = hashlib.sha256(self.token.encode()).hexdigest()
        return f"gitlab:{self.host}:{digest}"

    async def request(self, path: str, params=None):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("Expected a relative GitLab API path")
        # encoded=True preserves encoded slashes in project IDs and release tags.
        url = URL(self.base_url + path, encoded=True)
        if params:
            url = url.extend_query(params)
        status, headers, body = await self.transport.request(
            "GET", url, {"PRIVATE-TOKEN": self.token}
        )
        if not 200 <= status < 300:
            raise GitLabError(status, path)
        import json

        return json.loads(body), headers

    async def getitem(self, path: str):
        data, _ = await self.request(path)
        return data

    async def getiter(self, path: str, params=None):
        params = dict(params or {}, per_page=100, page=1)
        while True:
            data, headers = await self.request(path, dict(params))
            for item in data:
                yield item
            next_page = headers.get("x-next-page", headers.get("X-Next-Page"))
            if next_page == "":
                break
            if next_page is None:
                if len(data) < 100:
                    break
                next_page = params["page"] + 1
            page = int(next_page)
            if page <= params["page"]:
                raise ValueError("GitLab returned a non-advancing pagination cursor")
            params["page"] = page

    @staticmethod
    def project_path(repo: str) -> str:
        return f"/projects/{quote(repo, safe='')}"

    @staticmethod
    def user(data) -> User:
        return User(login=data["username"], html_url=data["web_url"])

    def item(self, data, repo: str, *, mr: bool):
        kind = "merge_requests" if mr else "issues"
        assignee = data.get("assignee") or next(iter(data.get("assignees") or []), None)
        common = dict(
            title=data["title"],
            user=self.user(data["author"]),
            labels=[Label(name=name) for name in data.get("labels", [])],
            html_url=data["web_url"],
            number=data["iid"],
            assignee=self.user(assignee) if assignee else None,
            body=data.get("description"),
            url=f"{self.base_url}{self.project_path(repo)}/{kind}/{data['iid']}",
            updated_at=data["updated_at"],
            created_at=data["created_at"],
            closed_at=data.get("merged_at") or data.get("closed_at"),
            draft=data.get("draft", data.get("work_in_progress", False)),
        )
        if not mr:
            return Issue(**common)
        return PullRequest(
            **common,
            merged_at=data.get("merged_at"),
            reviews_available=False,
            requested_reviewers=[self.user(u) for u in data.get("reviewers") or []],
        )

    @staticmethod
    def matches(item, with_labels, without_labels):
        labels = {label.name for label in item.labels}
        return set(with_labels) <= labels and not set(without_labels) & labels

    async def pull_diff(self, repo_name: str, number: int) -> str:
        parts = []
        async for entry in self.getiter(
            f"{self.project_path(repo_name)}/merge_requests/{number}/diffs"
        ):
            parts.append(
                f"--- a/{entry['old_path']}\n+++ b/{entry['new_path']}\n"
                + (entry.get("diff") or "[Diff unavailable]")
            )
        return "\n".join(parts)

    async def add_reviews(self, repo, prs):
        async def enrich(pr):
            try:
                data = await self.getitem(
                    f"{self.project_path(repo)}/merge_requests/{pr.number}/approvals"
                )
            except GitLabError as error:
                if error.status not in (404, 405):
                    raise
                return pr
            pr.reviews = [
                Review(user=self.user(entry["user"]), state="APPROVED")
                for entry in data.get("approved_by", [])
            ]
            pr.reviews_available = True
            return pr

        return await bounded_gather(prs, enrich, description="Fetching MR approvals")

    async def list_items(self, repo, *, mr, params, with_labels=(), without_labels=()):
        kind = "merge_requests" if mr else "issues"
        # Local filtering also handles commas and GitLab's reserved label names.
        items = await collect_items(
            self.getiter(f"{self.project_path(repo)}/{kind}", params),
            lambda payload: self.item(payload, repo, mr=mr),
            "Finding MRs" if mr else "Finding issues",
        )
        return [
            item for item in items if self.matches(item, with_labels, without_labels)
        ]

    async def merged_prs(
        self,
        repo_name: str,
        start: datetime,
        end: datetime,
        with_labels: Sequence[str] = (),
        without_labels: Sequence[str] = (),
    ) -> list[PullRequest]:
        prs = await self.list_items(
            repo_name,
            mr=True,
            params={
                "state": "merged",
                "scope": "all",
                "updated_after": start.isoformat(),
            },
            with_labels=with_labels,
            without_labels=without_labels,
        )
        prs = [
            pr
            for pr in prs
            if pr.merged_at is not None and start <= pr.merged_at <= end
        ]
        return await self.add_reviews(repo_name, prs)

    async def open_prs(
        self,
        repo_name: str,
        with_labels: Sequence[str] = (),
        without_labels: Sequence[str] = (),
        requirements: FetchRequirements = FetchRequirements(),
    ) -> list[PullRequest]:
        prs = await self.list_items(
            repo_name,
            mr=True,
            params={"state": "opened", "scope": "all"},
            with_labels=with_labels,
            without_labels=without_labels,
        )
        return await self.add_reviews(repo_name, prs) if requirements.reviews else prs

    async def open_issues(
        self,
        repo_name: str,
        with_labels: Sequence[str] = (),
        without_labels: Sequence[str] = (),
        start: datetime | None = None,
        end: datetime | None = None,
        include_prs: bool = False,
    ) -> list[Issue]:
        params = {"state": "opened", "scope": "all"}
        if start is not None:
            params["created_after"] = start.isoformat()
        if end is not None:
            params["created_before"] = end.isoformat()
        issues = await self.list_items(
            repo_name,
            mr=False,
            params=params,
            with_labels=with_labels,
            without_labels=without_labels,
        )
        if include_prs:
            prs = await self.list_items(
                repo_name,
                mr=True,
                params=params,
                with_labels=with_labels,
                without_labels=without_labels,
            )
            issues.extend(
                Issue(**pr.model_dump(), pull_request={"url": pr.url}) for pr in prs
            )
        return [
            item
            for item in issues
            if (start is None or item.created_at >= start)
            and (end is None or item.created_at <= end)
        ]

    async def release_prs(
        self,
        repo_name: str,
        release_ref: str,
        with_labels: Sequence[str] = (),
        without_labels: Sequence[str] = (),
    ) -> tuple[str, list[PullRequest]]:
        tag = parse_release_tag(release_ref, repo_name, self.host)
        root = self.project_path(repo_name)
        release = await self.getitem(f"{root}/releases/{quote(tag, safe='')}")
        numbers = extract_release_numbers(
            release.get("description") or "", repo_name, self.host
        )
        if not numbers:
            raise ValueError(f"No MR references for '{repo_name}' in release '{tag}'")

        async def fetch(number):
            return await self.getitem(f"{root}/merge_requests/{number}")

        payloads = await bounded_gather(
            numbers, fetch, description="Fetching release MRs"
        )
        prs = [
            self.item(payload, repo_name, mr=True)
            for payload in payloads
            if payload.get("state") == "merged"
        ]
        prs = [pr for pr in prs if self.matches(pr, with_labels, without_labels)]
        return tag, await self.add_reviews(repo_name, prs)
