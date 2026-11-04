import json
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading
from unittest.mock import AsyncMock, Mock

from unittest.mock import AsyncMock, Mock

import aiohttp
import gidgethub
from keyring.errors import KeyringError
import pytest
import typer
from typer.testing import CliRunner

import mtng.auth as auth
import mtng.cli
from mtng.auth import llm_keyring_username, resolve_llm_api_key
from mtng.forge.gitlab import GitLabError


@pytest.fixture(params=[("github", "github.com"), ("gitlab", "gitlab.cern.ch")])
def target(request):
    return auth.AuthTarget(*request.param)


def test_resolution_precedence_and_existing_keyring(monkeypatch, target):
    monkeypatch.setenv("GH_TOKEN", " github-env ")
    monkeypatch.setenv("GITLAB_TOKEN", " gitlab-env ")
    get = Mock(return_value=" stored-token ")
    monkeypatch.setattr(auth.keyring, "get_password", get)
    assert auth.resolve_token(" explicit ", target) == ("explicit", "--token")
    get.assert_not_called()
    assert auth.resolve_token(" ", target) == (f"{target.forge}-env", target.env_var)
    get.assert_not_called()
    monkeypatch.setenv(target.env_var, " ")
    assert auth.resolve_token(None, target) == ("stored-token", "system keychain")
    username = (
        "github-token" if target.forge == "github" else "gitlab:gitlab.cern.ch:token"
    )
    get.assert_called_once_with("mtng", username)
    monkeypatch.setenv(target.env_var, "ignored")
    assert auth.resolve_token(None, target, allow_env=False) == (
        "stored-token",
        "system keychain",
    )


@pytest.mark.parametrize("stored", [None, "", " "])
def test_missing_token(monkeypatch, target, stored):
    monkeypatch.delenv(target.env_var, raising=False)
    monkeypatch.setattr(auth.keyring, "get_password", Mock(return_value=stored))
    with pytest.raises(typer.BadParameter, match="No .* token provided"):
        auth.resolve_token(None, target)


@pytest.mark.parametrize("operation", ["read", "write"])
def test_keyring_failure(monkeypatch, target, operation):
    monkeypatch.delenv(target.env_var, raising=False)
    monkeypatch.setattr(
        auth.keyring, "get_password", Mock(side_effect=KeyringError("unavailable"))
    )
    monkeypatch.setattr(
        auth.keyring, "set_password", Mock(side_effect=KeyringError("unavailable"))
    )
    with pytest.raises(typer.BadParameter, match="system keychain"):
        if operation == "read":
            auth.resolve_token(None, target)
        else:
            auth.store_token("secret", target)


@pytest.mark.parametrize("validate", [True, False])
def test_login_preserves_credentials(monkeypatch, target, validate):
    store = Mock()
    validator = AsyncMock(return_value="alice")
    monkeypatch.setattr(auth.keyring, "set_password", store)
    monkeypatch.setattr(mtng.cli, "validate_token", validator)
    result = CliRunner().invoke(
        mtng.cli.cli,
        [
            "auth",
            "login",
            "--host",
            target.host,
            "--token",
            " secret ",
            "--validate" if validate else "--no-validate",
        ],
    )
    assert result.exit_code == 0, result.output
    username = (
        "github-token" if target.forge == "github" else "gitlab:gitlab.cern.ch:token"
    )
    store.assert_called_once_with("mtng", username, "secret")
    if validate:
        validator.assert_awaited_once_with("secret", target)
    else:
        validator.assert_not_called()
    assert "secret" not in result.output


@pytest.mark.parametrize("command", ["check", "status"])
def test_check_and_status_resolve_once(monkeypatch, target, command):
    resolver = Mock(return_value=("secret", target.env_var))
    validator = AsyncMock(return_value="alice")
    monkeypatch.setattr(mtng.cli, "resolve_token", resolver)
    monkeypatch.setattr(mtng.cli, "validate_token", validator)
    args = ["auth", command, "--host", target.host]
    if command == "status":
        args.append("--validate")
    result = CliRunner().invoke(mtng.cli.cli, args)
    assert result.exit_code == 0, result.output
    resolver.assert_called_once_with(None, target)
    validator.assert_awaited_once_with("secret", target)
    assert f"{target.label} token is valid for @alice." in result.output
    assert "secret" not in result.output


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["network", "timeout", "api"])
async def test_validation_errors_are_consistent(monkeypatch, target, failure):
    errors = {
        "network": aiohttp.ClientConnectionError("offline"),
        "timeout": TimeoutError(),
        "api": (
            gidgethub.BadRequest(401, "Bad credentials")
            if target.forge == "github"
            else GitLabError(401, "/user")
        ),
    }
    method = (
        "mtng.auth.GitHubAPI.getitem"
        if target.forge == "github"
        else "mtng.auth.GitLabForge.request"
    )
    monkeypatch.setattr(method, AsyncMock(side_effect=errors[failure]))
    with pytest.raises(typer.BadParameter, match="token validation failed"):
        await auth.validate_token("secret", target)


@pytest.mark.asyncio
async def test_validation_provider_response(monkeypatch, target):
    github = AsyncMock(return_value={"login": "octocat"})
    gitlab = AsyncMock(return_value=({"username": "alice"}, {}))
    monkeypatch.setattr("mtng.auth.GitHubAPI.getitem", github)
    monkeypatch.setattr("mtng.auth.GitLabForge.request", gitlab)
    assert await auth.validate_token("secret", target) == (
        "octocat" if target.forge == "github" else "alice"
    )
    used, unused = (github, gitlab) if target.forge == "github" else (gitlab, github)
    used.assert_awaited_once_with("/user")
    unused.assert_not_called()


def test_host_normalization_and_unsupported_targets():
    assert auth.AuthTarget.from_host("https://GITHUB.COM:443/") == auth.AuthTarget(
        "github", "github.com"
    )
    assert (
        auth.AuthTarget("gitlab", "https://GITLAB.CERN.CH:443/").keyring_username
        == "gitlab:gitlab.cern.ch:token"
    )
    with pytest.raises(ValueError, match="Enterprise"):
        auth.AuthTarget("github", "github.example")


def test_failed_login_does_not_store(monkeypatch, target):
    store = Mock()
    monkeypatch.setattr(auth.keyring, "set_password", store)
    monkeypatch.setattr(
        mtng.cli,
        "validate_token",
        AsyncMock(side_effect=typer.BadParameter("Invalid token")),
    )
    result = CliRunner().invoke(
        mtng.cli.cli, ["auth", "login", "--host", target.host, "--token", "secret"]
    )
    assert result.exit_code != 0
    store.assert_not_called()


def test_llm_keyring_username_validates_and_normalizes():
    assert (
        llm_keyring_username("https://LLM-Gateway.Example.com:8443/v1")
        == "llm:llm-gateway.example.com:8443:token"
    )
    assert (
        llm_keyring_username("http://localhost:4000/v1/") == "llm:localhost:4000:token"
    )
    assert (
        llm_keyring_username("https://gw.example.com/v1") == "llm:gw.example.com:token"
    )
    # An identity (e.g. a LiteLLM team) selects a distinct keychain entry.
    assert (
        llm_keyring_username("https://gw.example.com/v1", "team-b")
        == "llm:gw.example.com:team-b:token"
    )
    assert llm_keyring_username("https://gw.example.com/v1", None) == (
        llm_keyring_username("https://gw.example.com/v1", "")
    )
    with pytest.raises(ValueError, match="http"):
        llm_keyring_username("ftp://gw.example.com")
    with pytest.raises(ValueError, match="http"):
        llm_keyring_username("not a url")


def test_llm_api_key_resolution_precedence(monkeypatch):
    get = Mock(return_value=" stored-keyring-key ")
    monkeypatch.setattr(auth.keyring, "get_password", get)
    base_url = "https://gw.example.com/v1"
    # A set environment variable wins, like forge token resolution.
    monkeypatch.setenv("LITELLM_API_KEY", " env-key ")
    assert resolve_llm_api_key("LITELLM_API_KEY", base_url) == "env-key"
    get.assert_not_called()
    # Blank env values fall through to the keychain.
    monkeypatch.setenv("LITELLM_API_KEY", "  ")
    assert resolve_llm_api_key("LITELLM_API_KEY", base_url) == "stored-keyring-key"
    get.assert_called_once_with("mtng", "llm:gw.example.com:token")
    # No environment variable at all: keychain value is trimmed and used.
    monkeypatch.delenv("LITELLM_API_KEY")
    get = Mock(return_value="stored-keyring-key")
    monkeypatch.setattr(auth.keyring, "get_password", get)
    assert resolve_llm_api_key("LITELLM_API_KEY", base_url) == "stored-keyring-key"


@pytest.mark.parametrize("stored", [None, ""])
def test_llm_api_key_missing_stays_optional(monkeypatch, stored):
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.setattr(auth.keyring, "get_password", Mock(return_value=stored))
    assert resolve_llm_api_key("LITELLM_API_KEY", "https://gw.example.com/v1") is None


def test_llm_api_key_identity_selects_keychain_entry(monkeypatch):
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    get = Mock(return_value="team-b-key")
    monkeypatch.setattr(auth.keyring, "get_password", get)
    assert (
        resolve_llm_api_key("LITELLM_API_KEY", "https://gw.example.com/v1", "team-b")
        == "team-b-key"
    )
    get.assert_called_once_with("mtng", "llm:gw.example.com:team-b:token")
    # The environment variable still outranks a named identity's keychain entry.
    monkeypatch.setenv("LITELLM_API_KEY", "env-wins")
    assert (
        resolve_llm_api_key("LITELLM_API_KEY", "https://gw.example.com/v1", "team-b")
        == "env-wins"
    )


def test_llm_api_key_broken_keychain_is_optional(monkeypatch):
    from keyring.errors import KeyringError

    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    monkeypatch.setattr(
        auth.keyring, "get_password", Mock(side_effect=KeyringError("locked"))
    )
    assert resolve_llm_api_key("LITELLM_API_KEY", "https://gw.example.com/v1") is None


class _ModelsHandler(BaseHTTPRequestHandler):
    """Fake OpenAI-compatible /models endpoint for CLI validation tests."""

    def do_GET(self):  # noqa: N802 (http.server API)
        length = int(self.headers.get("Content-Length") or 0)
        if length:
            self.rfile.read(length)
        with open("/tmp/llm_models_seen.txt", "w") as seen:
            json.dump(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                },
                seen,
            )
        if self.headers.get("Authorization") != "Bearer good-key":
            self.send_response(401)
        else:
            self.send_response(200)
        body = json.dumps(
            {"data": [{"id": "m1"}, {"id": "m2"}, {"id": "m3"}, {"id": "m4"}]}
        ).encode()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def models_server():
    """A local OpenAI-compatible /models endpoint (loopback only)."""

    server = HTTPServer(("127.0.0.1", 0), _ModelsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/v1"
    server.shutdown()


@pytest.mark.local_http
def test_llm_validate_and_login(monkeypatch, models_server):
    monkeypatch.setattr(auth.keyring, "set_password", Mock())
    monkeypatch.setattr(auth.keyring, "get_password", Mock(return_value=None))
    # --no-validate: stored without contacting the gateway.
    result = CliRunner().invoke(
        mtng.cli.cli,
        [
            "auth",
            "llm",
            "login",
            models_server,
            "--identity",
            "team-b",
            "--key",
            "key",
            "--no-validate",
        ],
    )
    assert result.exit_code == 0, result.output
    expected_entry = llm_keyring_username(models_server, "team-b")
    auth.keyring.set_password.assert_called_once_with("mtng", expected_entry, "key")
    # Validation rejects a wrong key and stores nothing.
    result = CliRunner().invoke(
        mtng.cli.cli,
        ["auth", "llm", "login", models_server, "--key", "bad", "--identity", "t"],
    )
    assert result.exit_code != 0
    assert "rejected" in result.output
    # A correct key validates, reports the models, and stores the key.
    result = CliRunner().invoke(
        mtng.cli.cli,
        ["auth", "llm", "login", models_server, "--key", "good-key", "-i", "t"],
    )
    assert result.exit_code == 0, result.output
    assert "4 models available" in result.output
    auth.keyring.set_password.assert_called_with(
        "mtng", llm_keyring_username(models_server, "t"), "good-key"
    )


@pytest.mark.local_http
def test_llm_status(monkeypatch, models_server):
    runner = CliRunner()
    # Nothing configured: exit code 1 with guidance.
    monkeypatch.setattr(auth.keyring, "get_password", Mock(return_value=None))
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    result = runner.invoke(
        mtng.cli.cli, ["auth", "llm", "status", models_server, "-i", "team-b"]
    )
    assert result.exit_code == 1
    assert "not configured" in result.output
    assert "auth llm login" in result.output
    # Keychain entry is found and reported, and --validate hits the gateway.
    monkeypatch.setattr(auth.keyring, "get_password", Mock(return_value="good-key"))
    result = runner.invoke(
        mtng.cli.cli,
        ["auth", "llm", "status", models_server, "-i", "team-b", "--validate"],
    )
    assert result.exit_code == 0, result.output
    assert "stored in your system keychain" in result.output
    assert "4 models available" in result.output
    # An environment variable outranks the keychain in status too.
    monkeypatch.setenv("LITELLM_API_KEY", "env-key")
    result = runner.invoke(
        mtng.cli.cli,
        ["auth", "llm", "status", models_server, "-i", "team-b", "--validate"],
    )
    assert result.exit_code != 0  # env key is not the valid one
    assert "$LITELLM_API_KEY" in result.output


@pytest.mark.local_http
def test_llm_validate_rejects_unreachable_gateway(monkeypatch):
    import pytest

    monkeypatch.setattr(auth.keyring, "get_password", Mock(return_value=None))
    import asyncio as _asyncio

    with pytest.raises(typer.BadParameter, match="Could not reach the gateway"):
        _asyncio.run(auth.validate_llm_key("k", "http://127.0.0.1:1/v1"))
