import asyncio
from typing import Protocol
from datetime import datetime

from mtng.models import Issue, PullRequest


FETCH_CONCURRENCY = 8


class Forge(Protocol):
    """A code hosting service that mtng can collect PRs and issues from.

    Label filters are applied by the forge (server side where it can), so every
    method returns only items matching ``with_labels`` and ``without_labels``.
    """

    # Distinguishes forges, API hosts, and credential scopes. Never include
    # raw credentials. Instances with the same cache_id must see the same data.
    cache_id: str

    async def merged_prs(
        self,
        repo_name: str,
        start: datetime,
        end: datetime,
        with_labels: list[str] = [],
        without_labels: list[str] = [],
    ) -> list[PullRequest]: ...

    async def release_prs(
        self,
        repo_name: str,
        release_ref: str,
        with_labels: list[str] = [],
        without_labels: list[str] = [],
    ) -> tuple[str, list[PullRequest]]:
        """Resolve ``release_ref`` (a tag or release URL) and return the tag
        name with the PRs that landed in it."""
        ...

    async def open_prs(
        self,
        repo_name: str,
        with_labels: list[str] = [],
        without_labels: list[str] = [],
    ) -> list[PullRequest]: ...

    async def open_issues(
        self,
        repo_name: str,
        with_labels: list[str] = [],
        without_labels: list[str] = [],
        start: datetime | None = None,
        end: datetime | None = None,
        include_prs: bool = False,
    ) -> list[Issue]:
        """Open issues, optionally restricted to a creation window. With
        ``include_prs``, open PRs are returned too, as plain ``Issue``s."""
        ...


async def bounded_gather(items, fn, concurrency: int = FETCH_CONCURRENCY):
    sem = asyncio.Semaphore(concurrency)

    async def wrapped(item):
        async with sem:
            return await fn(item)

    return await asyncio.gather(*(wrapped(item) for item in items))
