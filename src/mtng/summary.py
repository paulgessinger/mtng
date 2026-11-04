"""Optional LLM summaries, cached by exact prompt and transport indefinitely."""

import asyncio
import hashlib
import json
import random
import subprocess
import time
from pathlib import Path

import appdirs
import diskcache
import requests

from mtng.auth import resolve_llm_api_key
from mtng.models import IssueBase
from mtng.progress import progress_step
from mtng.spec import ChatCompletionsBackend, CommandBackend, LLMConfig

TITLE_VERDICT_PROTOCOL = (
    "Begin your reply with USEFUL if the title alone already conveys the "
    "essence of the change, or with REDUNDANT if the summary adds meaningful "
    "information the title lacks. On the very next line, provide the summary "
    "itself, formatted exactly as requested below."
)


def build_prompt(item: IssueBase, diff: str, config: LLMConfig) -> str:
    def truncate(text, limit):
        return text[:limit] + ("\n[Truncated]" if len(text) > limit else "")

    content = {
        "title": item.title,
        "description": truncate(item.body or "", config.max_description_chars),
        "diff": truncate(diff, config.max_diff_chars),
    }
    protocol = TITLE_VERDICT_PROTOCOL if config.title_check else ""
    return (
        config.instructions
        + (f"\n\n{protocol}" if protocol else "")
        + "\n\nPR content (JSON):\n"
        + json.dumps(content, ensure_ascii=False)
    )


def split_verdict(output: str) -> tuple[bool | None, str]:
    """Split an optional ``USEFUL``/``REDUNDANT`` verdict from the summary text.

    Returns ``(title_ok, summary)``; ``title_ok`` is ``None`` when the output
    carries no verdict. Malformed verdict prefixes degrade gracefully: only a
    bare first line containing exactly one of the two keywords is consumed.
    """

    lines = output.splitlines()
    if lines:
        first = lines[0].strip().upper().rstrip(".:!")
        if first in ("USEFUL", "REDUNDANT"):
            return first == "USEFUL", " ".join("\n".join(lines[1:]).split())
    return None, " ".join(output.split())


def cache_key(transport, prompt: str) -> str:
    return hashlib.sha256(
        json.dumps(
            [transport.model_dump(), prompt], ensure_ascii=False, sort_keys=True
        ).encode("utf-8")
    ).hexdigest()


def summarize_command(prompt: str, transport: CommandBackend, timeout: float) -> str:
    try:
        result = subprocess.run(
            transport.command,
            input=prompt,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=timeout,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"LLM summary command failed: {error}") from error
    return result.stdout


RETRYABLE_STATUSES = frozenset({429, 502, 503, 504})
RETRY_AFTER_CAP = 30.0


def retry_delay(headers, attempt: int) -> float:
    """Light exponential back-off (~1 s, 2 s, ...) with jitter to keep
    concurrent workers from retrying in lockstep; an explicit Retry-After
    header wins, capped to keep a stuck gateway from stalling a run."""

    if (value := headers.get("retry-after")) is not None:
        try:
            return min(float(value), RETRY_AFTER_CAP)
        except ValueError:  # HTTP-date or garbage: fall through to back-off
            pass
    return 2**attempt + random.uniform(0, 0.5)


def summarize_chat_completions(
    prompt: str, transport: ChatCompletionsBackend, timeout: float, retries: int = 0
) -> str:
    """Blocking call; summarize() runs it from a worker thread.

    Transient gateway failures (429, 502, 503, 504 and dropped connections)
    are retried up to ``retries`` times with light back-off; other statuses
    fail immediately.
    """

    headers = {}
    if api_key := resolve_llm_api_key(
        transport.api_key_env, transport.base_url, transport.identity
    ):
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": transport.model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if transport.temperature is not None:
        payload["temperature"] = transport.temperature
    if transport.max_tokens is not None:
        payload["max_tokens"] = transport.max_tokens
    url = f"{transport.base_url}/chat/completions"
    for attempt in range(retries + 1):
        try:
            response = requests.post(
                url, json=payload, headers=headers, timeout=timeout
            )
        except requests.ConnectionError as error:
            if attempt == retries:
                raise ValueError(f"LLM summary request failed: {error}") from error
            time.sleep(retry_delay({}, attempt))
            continue
        except requests.RequestException as error:
            raise ValueError(f"LLM summary request failed: {error}") from error
        body = (
            response.json()
            if response.headers.get("content-type", "").startswith("application/json")
            else None
        )
        if response.status_code == 200:
            return body["choices"][0]["message"]["content"]
        if response.status_code in RETRYABLE_STATUSES and attempt < retries:
            time.sleep(retry_delay(response.headers, attempt))
            continue
        detail = body.get("error", body) if isinstance(body, dict) else response.text
        raise ValueError(
            f"LLM summary request failed ({response.status_code}): {detail}"
        )


def summarize(prompt: str, config: LLMConfig, cache) -> tuple[bool | None, str]:
    key = cache_key(config.transport, prompt)
    cached = cache.get(key)
    if cached is not None:
        # Entries written before the title verdict existed are plain strings.
        return cached if isinstance(cached, tuple) else (None, cached)
    transport = config.transport
    match transport:
        case CommandBackend():
            output = summarize_command(prompt, transport, config.timeout)
        case ChatCompletionsBackend():
            output = summarize_chat_completions(
                prompt, transport, config.timeout, config.retries
            )
    title_ok, summary = split_verdict(output)
    if not summary:
        raise ValueError("LLM summary command returned empty output")
    cache.set(key, (title_ok, summary), expire=None)
    return title_ok, summary


def report_prs(data):
    """Visit every PR occurrence, including category groups and repeated sections."""

    def items(value):
        if isinstance(value, IssueBase):
            if value.is_pr:
                yield value
        elif isinstance(value, dict):
            for child in value.values():
                yield from items(child)
        elif isinstance(value, list):
            for child in value:
                yield from items(child)

    for block in data.values():
        for item in items(block):
            yield block["spec"], item


async def add_summaries(data, forges, config: LLMConfig) -> dict[str, str]:
    """Summarize every distinct PR in the report, in parallel.

    Distinct PRs are summarized once and attached to every occurrence;
    failures propagate and abort the whole run, leaving the cache free of
    incomplete results. With ``config.title_check``, a model verdict that the
    title alone is sufficient ("REDUNDANT") suppresses the summary line.
    Concurrency is bounded by ``config.max_concurrency``
    and covers diff fetching too, since that is also an awaited network call.
    """

    if not config.enabled:
        return {}
    work = []
    seen = set()
    for repo, item in report_prs(data):
        identity = (repo.forge_key, repo.name, item.number, item.title, item.body)
        if identity not in seen:
            seen.add(identity)
            work.append((repo, item, identity))
    if not work:
        return {}
    path = Path(appdirs.user_cache_dir("mtng")) / "llm-v1"
    # No time expiry or size-based eviction: entries remain until manually removed.
    with diskcache.Cache(str(path), eviction_policy="none") as cache:
        with progress_step("Summarizing PRs", total=len(work), unit="PRs") as step:
            semaphore = asyncio.Semaphore(config.max_concurrency)
            label = asyncio.Event()  # first completion swaps in a PR-specific label
            described = set()
            by_identity: dict = {}

            async def summarize_one(entry):
                repo, item, identity = entry
                async with semaphore:
                    backend = forges[repo.forge_key]
                    diff = (
                        await backend.pull_diff(repo.name, item.number)
                        if config.max_diff_chars
                        else ""
                    )
                    prompt = build_prompt(item, diff, config)
                    verdict = await asyncio.to_thread(summarize, prompt, config, cache)
                by_identity[identity] = verdict
                step.advance()
                if label.is_set() and (repo.name, item.number) not in described:
                    step.progress.update(
                        step.task_id,
                        description=f"Summarizing PRs: {repo.name}#{item.number}",
                    )
                    described.add((repo.name, item.number))

            pending = {
                asyncio.create_task(summarize_one(entry), name=repr(entry[2]))
                for entry in work
            }
            try:
                await asyncio.gather(*pending)
            finally:
                label.set()
                for task in pending:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        for repo, item in report_prs(data):
            identity = (repo.forge_key, repo.name, item.number, item.title, item.body)
            title_ok, summary = by_identity[identity]
            if title_ok is not False:
                item.llm_summary = summary
    return {
        item.html_url: item.llm_summary
        for _, item in report_prs(data)
        if item.llm_summary is not None
    }


def restore_summaries(data, summaries, config: LLMConfig):
    if config.enabled:
        for _, item in report_prs(data):
            item.llm_summary = summaries.get(item.html_url)
