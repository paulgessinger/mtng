import functools
import asyncio
import pickle
from typing import Protocol
from datetime import datetime

import diskcache
import appdirs

from mtng.models import Issue, PullRequest


cache = diskcache.Cache(appdirs.user_cache_dir("mtng"))

_CACHE_MISS = object()
FETCH_CONCURRENCY = 8

# Bump whenever a model stored in the cache gains or loses fields. Entries are
# pickled pydantic instances, so an old entry restored into a new class is
# missing the new attributes entirely and raises AttributeError on access.
CACHE_VERSION = 4


class Forge(Protocol):
    """A code hosting service that mtng can collect PRs and issues from.

    Label filters are applied by the forge (server side where it can), so every
    method returns only items matching ``with_labels`` and ``without_labels``.
    """

    # Distinguishes cache entries of different forges and hosts. Two forge
    # instances with the same cache_id must return the same data.
    cache_id: str

    async def merged_prs(
        self,
        repo_name: str,
        start: datetime,
        end: datetime,
        with_labels: list[str] = [],
        without_labels: list[str] = [],
    ) -> list[PullRequest]:
        ...

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
    ) -> list[PullRequest]:
        ...

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


def memoize(expire=0):
    """Cache a forge method's result on disk. The instance itself is left out
    of the key in favour of its ``cache_id``, since API clients don't pickle."""

    def decorator(fn):
        @functools.wraps(fn)
        async def wrapped(self, *args, **kwargs):
            key = (
                fn.__qualname__.encode("utf-8")
                + b"_v"
                + str(CACHE_VERSION).encode("utf-8")
                + b"_"
                + self.cache_id.encode("utf-8")
                + b"_"
                + pickle.dumps(args)
                + b"_"
                + pickle.dumps(kwargs)
            )

            hit = cache.get(key, default=_CACHE_MISS)
            if hit is not _CACHE_MISS:
                return hit

            result = await fn(self, *args, **kwargs)
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
