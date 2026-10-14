"""Credential-scoped HTTP response caching and per-origin request coordination."""

import asyncio
import hashlib
import json
import time
import weakref
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp
import appdirs
import diskcache
from gidgethub.aiohttp import GitHubAPI
from yarl import URL

from mtng.progress import progress_step

# Only JSON strings are persisted, never Python models or authorization headers.
cache = diskcache.Cache(str(Path(appdirs.user_cache_dir("mtng")) / "http-v1"))
RETAIN_SECONDS = 30 * 24 * 3600


class RateLimitError(ValueError):
    pass


@dataclass
class Coordinator:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: dict = field(default_factory=dict)
    not_before: float = 0


_coordinators = weakref.WeakKeyDictionary()


def coordinator(origin: str) -> Coordinator:
    scopes = _coordinators.setdefault(asyncio.get_running_loop(), {})
    return scopes.setdefault(origin, Coordinator())


def request_key(method, url, headers):
    parts = urlsplit(str(url))
    canonical = urlunsplit(
        (
            parts.scheme.lower(),
            parts.netloc.lower(),
            parts.path,
            urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True))),
            "",
        )
    )
    # Include all representation/auth headers, but exclude validators which
    # describe freshness rather than a different representation.
    scoped_headers = sorted(
        (k.lower(), v)
        for k, v in headers.items()
        if k.lower() not in {"if-none-match", "if-modified-since"}
    )
    return hashlib.sha256(
        json.dumps([method, canonical, scoped_headers]).encode()
    ).hexdigest()


def retry_delay(headers, attempt):
    retry = headers.get("retry-after")
    if retry:
        try:
            return max(0, float(retry))
        except ValueError:
            return max(0, parsedate_to_datetime(retry).timestamp() - time.time())
    if headers.get("x-ratelimit-remaining") == "0":
        try:
            return max(0, float(headers["x-ratelimit-reset"]) - time.time())
        except (KeyError, ValueError):
            pass
    return 60 * 2**attempt


class HttpTransport:
    def __init__(self, session, *, max_age=300, enabled=True, max_wait=60):
        self.session = session
        self.max_age = max_age
        self.enabled = enabled
        self.max_wait = max_wait
        self.stats = dict(hits=0, requests=0, revalidated=0, retries=0, shared=0)

    async def request(self, method, url, headers, body=b""):
        url = URL(url, encoded=True)
        if url.scheme != "https":
            raise ValueError("Forge API requests require HTTPS")
        headers = dict(headers)
        key = request_key(method, url, headers)
        scope = coordinator(str(url.origin()))
        # Freshness policy participates in in-flight deduplication so refresh
        # callers cannot accidentally join a request served from a fresh cache.
        pending_key = (key, self.enabled, self.max_age)
        if method != "GET":
            return await self._request(scope, key, method, url, headers, body)
        if pending_key in scope.pending:
            self.stats["shared"] += 1
            return await asyncio.shield(scope.pending[pending_key])
        task = asyncio.create_task(
            self._request(scope, key, method, url, headers, body)
        )
        scope.pending[pending_key] = task

        def finished(task):
            scope.pending.pop(pending_key, None)
            if not task.cancelled():
                task.exception()  # Retrieve errors even if every waiter cancelled.

        task.add_done_callback(finished)
        return await asyncio.shield(task)

    async def _request(self, scope, key, method, url, headers, body):
        stored = None
        if self.enabled and method == "GET":
            raw = cache.get(key)
            if raw is not None:
                try:
                    stored = json.loads(raw)
                    fresh = min(self.max_age, stored["max_age"])
                    if time.time() - stored["stored_at"] < fresh:
                        self.stats["hits"] += 1
                        return 200, stored["headers"], stored["body"].encode()
                except (ValueError, KeyError, TypeError):
                    stored = None
                    cache.delete(key)
        if stored:
            if stored["headers"].get("etag"):
                headers["If-None-Match"] = stored["headers"]["etag"]
            elif stored["headers"].get("last-modified"):
                headers["If-Modified-Since"] = stored["headers"]["last-modified"]

        async with scope.lock:
            waited = 0
            for attempt in range(3):
                wait = max(0, scope.not_before - time.time())
                if waited + wait > self.max_wait:
                    raise RateLimitError(
                        f"API at {url.host} is rate limited; retry in {wait:.0f}s (wait budget {self.max_wait}s)"
                    )
                if wait:
                    with progress_step(
                        f"Waiting {wait:.0f}s before retrying {url.host}",
                        total=1,
                        unit="waits",
                    ) as step:
                        await asyncio.sleep(wait)
                        step.advance()
                    waited += wait
                self.stats["requests"] += 1
                async with self.session.request(
                    method,
                    url,
                    headers=headers,
                    data=body,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as response:
                    status = response.status
                    response_headers = {
                        k.lower(): v for k, v in response.headers.items()
                    }
                    data = await response.read()
                limited = status == 429 or (
                    status == 403
                    and (
                        "retry-after" in response_headers
                        or response_headers.get("x-ratelimit-remaining") == "0"
                        or b"secondary rate limit" in data.lower()
                    )
                )
                if limited:
                    delay = retry_delay(response_headers, attempt)
                    scope.not_before = time.time() + delay
                elif status in (502, 503, 504):
                    delay = 2**attempt
                    scope.not_before = time.time() + delay
                else:
                    if response_headers.get("x-ratelimit-remaining") == "0":
                        scope.not_before = time.time() + retry_delay(
                            response_headers, attempt
                        )
                    break
                if attempt == 2:
                    if limited:
                        raise RateLimitError(
                            f"API at {url.host} remains rate limited after 3 attempts"
                        )
                    break
                self.stats["retries"] += 1
            if status == 304 and stored:
                self.stats["revalidated"] += 1
                response_headers = stored["headers"] | response_headers
                data = stored["body"].encode()
                status = 200
            if self.enabled and status in (401, 403):
                cache.delete(key)
            if self.enabled and method == "GET" and status == 200:
                self._store(key, response_headers, data)
            return status, response_headers, data

    def _store(self, key, headers, body):
        directives = {
            part.strip().lower() for part in headers.get("cache-control", "").split(",")
        }
        if "no-store" in directives or headers.get("vary") == "*":
            cache.delete(key)
            return
        max_age = 300
        for directive in directives:
            if directive.startswith("max-age="):
                try:
                    max_age = max(0, int(directive.split("=", 1)[1].strip('"')))
                except ValueError:
                    max_age = 0
        if "no-cache" in directives:
            max_age = 0
        # Keep pagination and validators, never cookies or arbitrary response
        # headers. Rate-limit headers describe the network, not cached reads.
        saved_headers = {
            k: v
            for k, v in headers.items()
            if k
            in {
                "content-type",
                "etag",
                "last-modified",
                "link",
                "x-next-page",
                "cache-control",
                "vary",
            }
        }
        try:
            decoded = body.decode("utf-8")
            json.loads(decoded)
        except (UnicodeDecodeError, ValueError):
            return
        cache.set(
            key,
            json.dumps(
                dict(
                    stored_at=time.time(),
                    max_age=max_age,
                    headers=saved_headers,
                    body=decoded,
                )
            ),
            expire=RETAIN_SECONDS,
        )


class CachedGitHubAPI(GitHubAPI):
    """Keep gidgethub's API/pagination handling, centralize HTTP policy."""

    def __init__(self, session, *args, transport=None, **kwargs):
        super().__init__(session, *args, **kwargs)
        self.transport = transport or HttpTransport(session)

    async def _request(self, method, url, headers, body=b""):
        if URL(url).origin() != URL(self.base_url).origin():
            raise ValueError("Refusing to forward GitHub credentials to another origin")
        return await self.transport.request(method, url, headers, body)
