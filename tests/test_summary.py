import json
from pathlib import Path
import subprocess
import sys
from unittest.mock import AsyncMock, Mock

import diskcache
import pytest
from pydantic import ValidationError

from mtng.generate import env
from mtng.models import PullRequest
from mtng.spec import ChatCompletionsBackend, CommandBackend, LLMConfig, Repository
from mtng.summary import (
    add_summaries,
    build_prompt,
    cache_key,
    restore_summaries,
    summarize,
)


@pytest.fixture(autouse=True)
def no_keychain(monkeypatch):
    """Keep tests away from the real system keychain."""

    monkeypatch.setattr("mtng.auth.keyring.get_password", Mock(return_value=None))


def pr():
    raw = json.loads((Path(__file__).parent / "ref/merged_prs.json").read_text())[0]
    return PullRequest.model_validate(raw)


@pytest.mark.parametrize(
    "values",
    [
        {"transport": {"type": "command", "command": []}},
        {"transport": {"type": "command", "command": [""]}},
        {"transport": {"type": "command", "command": ["x\0"]}},
        {"transport": {"type": "chat-completions", "model": ""}},
        {
            "transport": {
                "type": "chat-completions",
                "model": "m",
                "base_url": "ftp://x",
            }
        },
        {
            "transport": {
                "type": "chat-completions",
                "model": "m",
                "identity": "bad identity!",
            }
        },
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
    config = LLMConfig(transport=CommandBackend(command=[sys.executable, str(script)]))
    path = str(tmp_path / "cache")
    with diskcache.Cache(path, eviction_policy="none") as cache:
        assert summarize("first prompt", config, cache) == "Summary: first prompt"
    with diskcache.Cache(path, eviction_policy="none") as cache:
        assert summarize("first prompt", config, cache) == "Summary: first prompt"
        assert counter.read_text() == "x"
        summarize("changed prompt", config, cache)
        changed = config.model_copy(
            update={
                "transport": CommandBackend(
                    command=config.transport.command + ["changed-model"]
                )
            }
        )
        summarize("first prompt", changed, cache)
        assert counter.read_text() == "xxx"
        for key in cache:
            _, expires = cache.get(key, expire_time=True)
            assert expires is None


def test_cache_key_separates_transports():
    command = CommandBackend(command=["llm"])
    gateway = CommandBackend(command=["llm", "--model", "other"])
    chat = ChatCompletionsBackend(model="gpt-4o", base_url="https://gw.example.com/v1")
    prompt = "same prompt"
    assert cache_key(command, prompt) != cache_key(gateway, prompt)
    assert cache_key(command, prompt) != cache_key(chat, prompt)


def test_chat_completions(monkeypatch, tmp_path):
    captured = {}
    calls = []

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self, url, **kwargs):
            calls.append(url)
            captured.update(url=url, payload=kwargs["json"], headers=kwargs["headers"])

        def json(self):
            return {"choices": [{"message": {"content": "  Compact\n summary.  "}}]}

    post = Response

    transport = {
        "type": "chat-completions",
        "model": "claude-sonnet-4",
        "base_url": "https://gateway.example.com/v1/",
        "api_key_env": "GATEWAY_KEY",
    }
    config = LLMConfig(enabled=True, transport=transport)
    assert config.transport.base_url == "https://gateway.example.com/v1"
    monkeypatch.delenv("GATEWAY_KEY", raising=False)
    monkeypatch.setattr("mtng.summary.requests.post", post)
    with diskcache.Cache(str(tmp_path)) as cache:
        assert summarize("prompt", config, cache) == "Compact summary."
        assert captured["url"] == "https://gateway.example.com/v1/chat/completions"
        # No environment variable and no keychain entry: unauthenticated request.
        assert "Authorization" not in captured["headers"]
        assert captured["payload"]["model"] == "claude-sonnet-4"
        # Same transport, same prompt: served from cache without a second call.
        assert summarize("prompt", config, cache) == "Compact summary."
        assert len(calls) == 1


def test_chat_completions_uses_keychain_key(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.delenv("GATEWAY_KEY", raising=False)
    monkeypatch.setattr(
        "mtng.auth.keyring.get_password", Mock(return_value=" keyring-key ")
    )

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self, url, **kwargs):
            captured.update(url=url, headers=kwargs["headers"])

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr("mtng.summary.requests.post", Response)
    config = LLMConfig(
        enabled=True,
        transport={
            "type": "chat-completions",
            "model": "m",
            "base_url": "https://gateway.example.com/v1",
            "api_key_env": "GATEWAY_KEY",
        },
    )
    with diskcache.Cache(str(tmp_path)) as cache:
        assert summarize("prompt", config, cache) == "ok"
    assert captured["headers"]["Authorization"] == "Bearer keyring-key"


def test_chat_completions_identity_keychain_entry(monkeypatch, tmp_path):
    """A named identity reads llm:<host>:<identity>:token from the keychain."""

    captured = {}
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    get = Mock(return_value="team-b-key")
    monkeypatch.setattr("mtng.auth.keyring.get_password", get)

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self, url, **kwargs):
            captured.update(headers=kwargs["headers"])

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr("mtng.summary.requests.post", Response)
    config = LLMConfig(
        enabled=True,
        transport={
            "type": "chat-completions",
            "model": "m",
            "base_url": "https://gw.example.com/v1",
            "identity": "team-b",
        },
    )
    with diskcache.Cache(str(tmp_path)) as cache:
        assert summarize("prompt", config, cache) == "ok"
    get.assert_called_once_with("mtng", "llm:gw.example.com:team-b:token")
    assert captured["headers"]["Authorization"] == "Bearer team-b-key"


def test_chat_completions_error(monkeypatch, tmp_path):
    class Response:
        status_code = 429
        text = "rate limited"
        headers = {"content-type": "application/json"}

        def json(self):
            return {"error": {"message": "rate limited"}}

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr("mtng.summary.requests.post", lambda *a, **k: Response())
    config = LLMConfig(
        enabled=True,
        transport={
            "type": "chat-completions",
            "model": "m",
            "base_url": "http://localhost:4000/v1",
        },
    )
    with diskcache.Cache(str(tmp_path)) as cache:
        with pytest.raises(ValueError, match="429"):
            summarize("prompt", config, cache)
        assert len(cache) == 0


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
        transport=CommandBackend(
            command=[sys.executable, "-c", 'print("Short summary.")']
        ),
    )
    repo = Repository(name="owner/repo")
    backend = AsyncMock()
    await add_summaries(
        {"repo": {"spec": repo, "prs": [pr()]}}, {repo.forge_key: backend}, config
    )
    backend.pull_diff.assert_not_called()
