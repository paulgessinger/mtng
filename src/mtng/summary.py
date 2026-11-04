"""Optional LLM summaries, cached by exact prompt and transport indefinitely."""

import asyncio
import hashlib
import json
import subprocess
from pathlib import Path

import appdirs
import diskcache
import requests

from mtng.auth import resolve_llm_api_key
from mtng.models import IssueBase
from mtng.progress import progress_items
from mtng.spec import ChatCompletionsBackend, CommandBackend, LLMConfig


def build_prompt(item: IssueBase, diff: str, config: LLMConfig) -> str:
    def truncate(text, limit):
        return text[:limit] + ("\n[Truncated]" if len(text) > limit else "")

    content = {
        "title": item.title,
        "description": truncate(item.body or "", config.max_description_chars),
        "diff": truncate(diff, config.max_diff_chars),
    }
    return (
        config.instructions
        + "\n\nPR content (JSON):\n"
        + json.dumps(content, ensure_ascii=False)
    )


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


def summarize_chat_completions(
    prompt: str, transport: ChatCompletionsBackend, timeout: float
) -> str:
    """Blocking call; summarize() runs it from a worker thread."""

    headers = {}
    if api_key := resolve_llm_api_key(transport.api_key_env, transport.base_url):
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": transport.model,
        "messages": [{"role": "user", "content": prompt}],
    }
    if transport.temperature is not None:
        payload["temperature"] = transport.temperature
    if transport.max_tokens is not None:
        payload["max_tokens"] = transport.max_tokens
    try:
        response = requests.post(
            f"{transport.base_url}/chat/completions",
            json=payload,
            headers=headers,
            timeout=timeout,
        )
    except requests.RequestException as error:
        raise ValueError(f"LLM summary request failed: {error}") from error
    body = (
        response.json()
        if response.headers.get("content-type", "").startswith("application/json")
        else None
    )
    if response.status_code != 200:
        detail = body.get("error", body) if isinstance(body, dict) else response.text
        raise ValueError(
            f"LLM summary request failed ({response.status_code}): {detail}"
        )
    return body["choices"][0]["message"]["content"]


def summarize(prompt: str, config: LLMConfig, cache) -> str:
    key = cache_key(config.transport, prompt)
    cached = cache.get(key)
    if cached is not None:
        return cached
    transport = config.transport
    match transport:
        case CommandBackend():
            output = summarize_command(prompt, transport, config.timeout)
        case ChatCompletionsBackend():
            output = summarize_chat_completions(prompt, transport, config.timeout)
    summary = " ".join(output.split())
    if not summary:
        raise ValueError("LLM summary command returned empty output")
    cache.set(key, summary, expire=None)
    return summary


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
    if not config.enabled:
        return {}
    summaries = {}
    path = Path(appdirs.user_cache_dir("mtng")) / "llm-v1"
    # No time expiry or size-based eviction: entries remain until manually removed.
    with diskcache.Cache(str(path), eviction_policy="none") as cache:
        for repo, item in progress_items(
            list(report_prs(data)), "Summarizing PRs", unit="PRs"
        ):
            identity = (repo.forge_key, repo.name, item.number, item.title, item.body)
            if identity not in summaries:
                backend = forges[repo.forge_key]
                diff = (
                    await backend.pull_diff(repo.name, item.number)
                    if config.max_diff_chars
                    else ""
                )
                prompt = build_prompt(item, diff, config)
                summaries[identity] = await asyncio.to_thread(
                    summarize, prompt, config, cache
                )
            item.llm_summary = summaries[identity]
    return {item.html_url: item.llm_summary for _, item in report_prs(data)}


def restore_summaries(data, summaries, config: LLMConfig):
    if config.enabled:
        for _, item in report_prs(data):
            item.llm_summary = summaries.get(item.html_url)
