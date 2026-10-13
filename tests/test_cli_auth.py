from unittest.mock import AsyncMock, Mock

import aiohttp
import gidgethub
from keyring.errors import KeyringError
import pytest
import typer
from typer.testing import CliRunner

import mtng.auth as auth
import mtng.cli
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
