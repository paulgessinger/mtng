"""Optional subprocess summaries, cached by exact prompt and command indefinitely."""

import asyncio
import hashlib
import json
from pathlib import Path
import subprocess

import appdirs
import diskcache

from mtng.models import IssueBase
from mtng.progress import progress_items
from mtng.spec import LLMConfig


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


def summarize(prompt: str, config: LLMConfig, cache) -> str:
    key = hashlib.sha256(
        json.dumps([config.command, prompt], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    cached = cache.get(key)
    if cached is not None:
        return cached
    try:
        result = subprocess.run(
            config.command,
            input=prompt,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=config.timeout,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"LLM summary command failed: {error}") from error
    summary = " ".join(result.stdout.split())
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
