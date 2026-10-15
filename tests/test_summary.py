import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock

import diskcache
import pytest
from pydantic import ValidationError

from mtng.generate import env
from mtng.models import PullRequest
from mtng.spec import LLMConfig, Repository
from mtng.summary import add_summaries, build_prompt, restore_summaries, summarize


def pr():
    raw = json.loads((Path(__file__).parent / "ref/merged_prs.json").read_text())[0]
    return PullRequest.model_validate(raw)


@pytest.mark.parametrize(
    "values",
    [
        {"command": []},
        {"command": [""]},
        {"command": ["x\0"]},
        {"max_diff_chars": -1},
        {"max_description_chars": -1},
        {"timeout": 0},
    ],
)
def test_invalid_config(values):
    with pytest.raises(ValidationError):
        LLMConfig(**values)


def test_prompt_limits():
    item = pr()
    item.body = "description"
    config = LLMConfig(max_diff_chars=3, max_description_chars=4)
    prompt = build_prompt(item, "abcdef", config)
    data = json.loads(prompt.split("PR content (JSON):\n")[1])
    assert data == {
        "title": item.title,
        "description": "desc\n[Truncated]",
        "diff": "abc\n[Truncated]",
    }
    assert "concisel" in prompt
    item.body = None
    assert (
        json.loads(build_prompt(item, "", config).split("PR content (JSON):\n")[1])[
            "description"
        ]
        == ""
    )


def test_subprocess_and_persistent_cache(tmp_path):
    counter = tmp_path / "calls"
    script = tmp_path / "llm.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"p = Path({str(counter)!r})\n"
        'p.write_text(p.read_text() + "x" if p.exists() else "x")\n'
        'print("Summary:", sys.stdin.read())\n'
    )
    config = LLMConfig(command=[sys.executable, str(script)])
    path = str(tmp_path / "cache")
    with diskcache.Cache(path, eviction_policy="none") as cache:
        assert summarize("first prompt", config, cache) == "Summary: first prompt"
    with diskcache.Cache(path, eviction_policy="none") as cache:
        assert summarize("first prompt", config, cache) == "Summary: first prompt"
        assert counter.read_text() == "x"
        summarize("changed prompt", config, cache)
        config.command.append("changed-model")
        summarize("first prompt", config, cache)
        assert counter.read_text() == "xxx"
        for key in cache:
            _, expires = cache.get(key, expire_time=True)
            assert expires is None


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("missing"),
        subprocess.TimeoutExpired("llm", 1),
        subprocess.CalledProcessError(1, "llm"),
        None,
    ],
)
def test_failures_are_not_cached(monkeypatch, tmp_path, failure):
    def run(*args, **kwargs):
        if failure:
            raise failure
        return subprocess.CompletedProcess(args, 0, stdout=" \n ")

    monkeypatch.setattr("mtng.summary.subprocess.run", run)
    with diskcache.Cache(str(tmp_path)) as cache:
        with pytest.raises(ValueError, match="LLM summary command"):
            summarize("prompt", LLMConfig(), cache)
        assert len(cache) == 0


@pytest.mark.asyncio
async def test_enrichment_deduplicates_sections_and_restores_offline(
    monkeypatch, tmp_path
):
    monkeypatch.setattr("mtng.summary.appdirs.user_cache_dir", lambda _: str(tmp_path))
    item = pr()
    duplicate = item.model_copy(deep=True)
    repo = Repository(name="owner/repo")
    data = {
        "first": {"spec": repo, "open_prs": [item], "groups": [{"items": [item]}]},
        "second": {"spec": repo, "merged_prs": [duplicate]},
    }
    backend = AsyncMock()
    backend.pull_diff.return_value = "+new code"
    calls = []

    def run(*args, **kwargs):
        calls.append(kwargs["input"])
        return subprocess.CompletedProcess(args, 0, stdout="Fixes 50% of a_bug & more.")

    monkeypatch.setattr("mtng.summary.subprocess.run", run)
    assert await add_summaries(data, {}, LLMConfig()) == {}
    config = LLMConfig(enabled=True)
    saved = await add_summaries(data, {repo.forge_key: backend}, config)
    assert len(calls) == 1
    backend.pull_diff.assert_awaited_once_with(repo.name, item.number)
    assert item.llm_summary == duplicate.llm_summary
    await add_summaries(data, {repo.forge_key: backend}, config)
    assert len(calls) == 1
    item.llm_summary = None
    restore_summaries(data, saved, config)
    rendered = env.get_template("item.tex").render(
        item=item, spec=repo, mode="OPEN", extra=""
    )
    assert r"Fixes 50\% of a\_bug \& more." in rendered
    assert rendered.index("Fixes") > rendered.index("by ")


@pytest.mark.asyncio
async def test_zero_diff_limit_skips_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr("mtng.summary.appdirs.user_cache_dir", lambda _: str(tmp_path))
    config = LLMConfig(
        enabled=True,
        max_diff_chars=0,
        command=[sys.executable, "-c", 'print("Short summary.")'],
    )
    repo = Repository(name="owner/repo")
    backend = AsyncMock()
    await add_summaries(
        {"repo": {"spec": repo, "prs": [pr()]}}, {repo.forge_key: backend}, config
    )
    backend.pull_diff.assert_not_called()
