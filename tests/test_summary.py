import asyncio
import json
from pathlib import Path
import requests
import subprocess
import sys
import time
import unittest.mock
from unittest.mock import AsyncMock, Mock

import diskcache
import pytest
from pydantic import ValidationError

from mtng.generate import env
from mtng.models import PullRequest
from mtng.spec import ChatCompletionsBackend, CommandBackend, LLMConfig, Repository
from mtng.summary import (
    TITLE_VERDICT_PROTOCOL,
    add_summaries,
    build_prompt,
    cache_key,
    restore_summaries,
    split_verdict,
    summarize,
    summary_targets,
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
        {"max_concurrency": 0},
        {"retries": -1},
    ],
)
def test_invalid_config(values):
    with pytest.raises(ValidationError):
        LLMConfig(**values)


def test_prompt_title_check_protocol():
    item = pr()
    off = build_prompt(item, "diff", LLMConfig(title_check=False))
    assert "USEFUL" not in off
    assert TITLE_VERDICT_PROTOCOL not in off
    on = build_prompt(item, "diff", LLMConfig())
    assert "USEFUL" in on and "REDUNDANT" in on
    assert TITLE_VERDICT_PROTOCOL in on
    assert json.loads(on.split("PR content (JSON):\n")[1])["title"] == item.title


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ("USEFUL\nShort summary.", (True, "Short summary.")),
        ("REDUNDANT.\nAdds context.", (False, "Adds context.")),
        ("useful:\nTidy text", (True, "Tidy text")),
        ("Just a summary", (None, "Just a summary")),
        ("USEFUL", (True, "")),
        ("USEFUL\nTwo\nlines", (True, "Two lines")),
        ("USEFUL BUT\nBroken", (None, "USEFUL BUT Broken")),
    ],
)
def test_split_verdict(output, expected):
    assert split_verdict(output) == expected


def test_summarize_reads_legacy_string_cache(tmp_path):
    config = LLMConfig()
    with diskcache.Cache(str(tmp_path)) as cache:
        cache.set(
            cache_key(
                config.transport,
                "prompts-unchanged",
            ),
            "Legacy summary.",
        )
        assert summarize("prompts-unchanged", config, cache) == (
            None,
            "Legacy summary.",
        )


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
    assert "15 words" in prompt
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
        assert summarize("first prompt", config, cache) == (
            None,
            "Summary: first prompt",
        )
    with diskcache.Cache(path, eviction_policy="none") as cache:
        assert summarize("first prompt", config, cache) == (
            None,
            "Summary: first prompt",
        )
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
        assert summarize("prompt", config, cache) == (None, "Compact summary.")
        assert captured["url"] == "https://gateway.example.com/v1/chat/completions"
        # No environment variable and no keychain entry: unauthenticated request.
        assert "Authorization" not in captured["headers"]
        assert captured["payload"]["model"] == "claude-sonnet-4"
        # Same transport, same prompt: served from cache without a second call.
        assert summarize("prompt", config, cache) == (None, "Compact summary.")
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
        assert summarize("prompt", config, cache) == (None, "ok")
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
        assert summarize("prompt", config, cache) == (None, "ok")
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
        retries=0,
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


@pytest.fixture
def retry_env(monkeypatch):
    """Deterministic back-off: record sleeps, strip jitter."""

    sleeps = []
    monkeypatch.setattr("mtng.summary.time.sleep", sleeps.append)
    monkeypatch.setattr("mtng.summary.random.uniform", lambda low, high: 0)
    return sleeps


def transient_post(calls, statuses, bodies):
    """Build a requests.post replacement cycling through scripted replies."""

    replies = iter(zip(statuses, bodies))

    def post(url, **kwargs):
        calls.append(url)
        status, body = next(replies)
        response = requests.Response()
        response.status_code = status
        response.headers["content-type"] = "application/json"
        response._content = json.dumps(body).encode()
        return response

    return post


def retry_config(**overrides):
    return LLMConfig(
        enabled=True,
        retries=2,
        transport={
            "type": "chat-completions",
            "model": "m",
            "base_url": "https://gw.example.com/v1",
        },
        **overrides,
    )


def test_chat_completions_retries_transient_statuses(monkeypatch, retry_env, tmp_path):
    calls = []
    monkeypatch.setattr(
        "mtng.summary.requests.post",
        transient_post(
            calls,
            [502, 503, 200],
            [
                {"error": "down"},
                {"error": "down"},
                {"choices": [{"message": {"content": "Recovered."}}]},
            ],
        ),
    )
    with diskcache.Cache(str(tmp_path)) as cache:
        assert summarize("prompt", retry_config(), cache) == (None, "Recovered.")
    assert len(calls) == 3
    assert retry_env == [1, 2]  # 2**attempt + jitter(0)


def test_chat_completions_honors_retry_after(monkeypatch, retry_env, tmp_path):
    calls = []
    import mtng.summary as summary_module

    inner = transient_post(
        calls,
        [200],
        [{"choices": [{"message": {"content": "ok"}}]}],
    )

    def post(url, **kwargs):
        if len(calls) == 0:
            calls.append(url)
            response = requests.Response()
            response.status_code = 429
            response.headers["content-type"] = "application/json"
            response.headers["retry-after"] = "7"
            response._content = b'{"error": "rate limited"}'
            return response
        return inner(url, **kwargs)

    monkeypatch.setattr(summary_module.requests, "post", post)
    with diskcache.Cache(str(tmp_path)) as cache:
        assert summarize("prompt", retry_config(), cache) == (None, "ok")
    assert len(calls) == 2
    assert retry_env == [7.0]


def test_chat_completions_retries_exhausted(monkeypatch, retry_env, tmp_path):
    calls = []
    monkeypatch.setattr(
        "mtng.summary.requests.post",
        transient_post(calls, [502, 502, 502], [{"error": "down"}] * 3),
    )
    with diskcache.Cache(str(tmp_path)) as cache:
        with pytest.raises(ValueError, match="502"):
            summarize("prompt", retry_config(), cache)
        assert len(calls) == 3
        assert retry_env == [1, 2]
        assert len(cache) == 0


def test_chat_completions_retries_connection_errors(monkeypatch, retry_env, tmp_path):
    calls = []

    def flaky(url, **kwargs):
        calls.append(url)
        if len(calls) < 3:
            raise requests.ConnectionError("connection reset")
        response = requests.Response()
        response.status_code = 200
        response.headers["content-type"] = "application/json"
        response._content = b'{"choices": [{"message": {"content": "ok"}}]}'
        return response

    monkeypatch.setattr("mtng.summary.requests.post", flaky)
    with diskcache.Cache(str(tmp_path)) as cache:
        assert summarize("prompt", retry_config(), cache) == (None, "ok")
    assert len(calls) == 3


def test_chat_completions_client_errors_fail_fast(monkeypatch, retry_env, tmp_path):
    calls = []
    monkeypatch.setattr(
        "mtng.summary.requests.post",
        transient_post(calls, [401], [{"error": {"message": "bad key"}}]),
    )
    with diskcache.Cache(str(tmp_path)) as cache:
        with pytest.raises(ValueError, match="401"):
            summarize("prompt", retry_config(), cache)
    assert len(calls) == 1
    assert retry_env == []


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
async def test_summary_targets_selection(monkeypatch, tmp_path):
    """llm_summary flags select sections; default stays summarize-everything."""

    monkeypatch.setattr("mtng.summary.appdirs.user_cache_dir", lambda _: str(tmp_path))
    repo = Repository(name="owner/repo")
    config = LLMConfig(
        enabled=True,
        transport=CommandBackend(
            command=[sys.executable, "-c", 'print("Section summary.")']
        ),
    )

    def block(flag):
        item = pr()
        return {
            "spec": repo,
            "prs": [item],
            "llm_summary": flag,
            "item": item,  # direct handle for assertions
        }

    # No flags anywhere: every block is summarized (legacy behavior).
    data = {"a": block(None), "b": block(None)}
    await add_summaries(data, {repo.forge_key: backend_stub()}, config)
    assert data["a"]["item"].llm_summary == "Section summary."
    assert data["b"]["item"].llm_summary == "Section summary."

    # One opt-in: only that block is summarized.
    data = {"a": block(None), "b": block(True), "c": block(None)}
    await add_summaries(data, {repo.forge_key: backend_stub()}, config)
    assert data["a"]["item"].llm_summary is None
    assert data["b"]["item"].llm_summary == "Section summary."
    assert data["c"]["item"].llm_summary is None

    # Opt-out only: everyone but the excluded block.
    data = {"a": block(None), "b": block(False)}
    await add_summaries(data, {repo.forge_key: backend_stub()}, config)
    assert data["a"]["item"].llm_summary == "Section summary."
    assert data["b"]["item"].llm_summary is None


def backend_stub():
    backend = AsyncMock()
    backend.pull_diff = AsyncMock(return_value="")
    return backend


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


@pytest.mark.asyncio
async def test_summaries_run_with_bounded_concurrency(monkeypatch, tmp_path):
    """Semaphore bounds in-flight work; failures abort without caching."""

    monkeypatch.setattr("mtng.summary.appdirs.user_cache_dir", lambda _: str(tmp_path))
    config = LLMConfig(
        enabled=True,
        max_concurrency=3,
        transport=CommandBackend(
            command=[sys.executable, "-c", 'print("Concise summary.")']
        ),
    )
    repo = Repository(name="owner/repo")
    items = [pr() for _ in range(7)]
    for index, item in enumerate(items):
        item.number = index + 1
        item.title = f"PR number {index + 1}"  # distinct identities
    backend = AsyncMock()
    state = {"in_flight": 0, "peak": 0}

    async def pull_diff(*args):
        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        await asyncio.sleep(0.05)
        state["in_flight"] -= 1
        return "+code"

    backend.pull_diff = pull_diff
    await add_summaries(
        {"repo": {"spec": repo, "prs": items}}, {repo.forge_key: backend}, config
    )
    assert state["peak"] == 3
    assert all(item.llm_summary == "Concise summary." for item in items)


@pytest.mark.asyncio
async def test_first_failure_aborts_pending_summaries(monkeypatch, tmp_path):
    """One failing summary cancels the rest and leaves the cache empty."""

    monkeypatch.setattr("mtng.summary.appdirs.user_cache_dir", lambda _: str(tmp_path))
    config = LLMConfig(
        enabled=True,
        max_concurrency=2,
        transport=CommandBackend(
            command=[sys.executable, "-c", "import sys; sys.exit(1)"]
        ),
    )
    repo = Repository(name="owner/repo")
    items = [pr() for _ in range(4)]
    for index, item in enumerate(items):
        item.number = index + 1
        item.title = f"PR number {index + 1}"
    backend = AsyncMock()
    started = []

    async def pull_diff(repo_name, number):
        started.append(number)
        await asyncio.sleep(0.02)
        return ""

    backend.pull_diff = pull_diff
    started.clear()
    with diskcache.Cache(str(tmp_path / "llm-v1")) as cache:
        with pytest.raises(ValueError, match="LLM summary command"):
            with unittest.mock.patch(
                "mtng.summary.summarize",
                side_effect=lambda *a, **k: (_ for _ in ()).throw(
                    ValueError("LLM summary command failed")
                ),
            ):
                await add_summaries(
                    {"repo": {"spec": repo, "prs": items}},
                    {repo.forge_key: backend},
                    config,
                )
        assert len(cache) == 0
    assert len(started) < 4  # pending work was cancelled before starting
    assert all(item.llm_summary is None for item in items)


@pytest.mark.asyncio
async def test_parallel_summaries_order_independent(monkeypatch, tmp_path):
    """Results attach to the right PRs regardless of completion order."""

    monkeypatch.setattr("mtng.summary.appdirs.user_cache_dir", lambda _: str(tmp_path))
    config = LLMConfig(
        enabled=True,
        max_concurrency=4,
        transport=CommandBackend(
            command=[sys.executable, "-c", 'print("Slow PR summary.")']
        ),
    )
    repo = Repository(name="owner/repo")
    items = [pr() for _ in range(4)]
    for index, item in enumerate(items):
        item.number = index + 1
        item.title = f"PR number {index + 1}"
    backend = AsyncMock()

    async def pull_diff(repo_name, number):
        await asyncio.sleep(0.01 * (4 - number))  # later PRs finish first
        return ""

    backend.pull_diff = pull_diff
    await add_summaries(
        {"repo": {"spec": repo, "prs": items}}, {repo.forge_key: backend}, config
    )
    assert all(item.llm_summary == "Slow PR summary." for item in items)
