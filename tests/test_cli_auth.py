import pytest
import typer
from typer.testing import CliRunner

import mtng.cli


class DummyGitHubError(Exception):
    def __init__(self, status_code: int, text: str):
        super().__init__(text)
        self.status_code = status_code


def test_resolve_github_token_prefers_cli_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GH_TOKEN", "env-token")
    monkeypatch.setattr("mtng.cli.get_keyring_token", lambda: "keyring-token")

    assert mtng.cli.resolve_github_token("cli-token") == "cli-token"


def test_resolve_github_token_prefers_env_over_keyring(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GH_TOKEN", "env-token")
    monkeypatch.setattr("mtng.cli.get_keyring_token", lambda: "keyring-token")

    assert mtng.cli.resolve_github_token(None) == "env-token"


def test_resolve_github_token_uses_keyring(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr("mtng.cli.get_keyring_token", lambda: "keyring-token")

    assert mtng.cli.resolve_github_token(None) == "keyring-token"


def test_resolve_github_token_with_source(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr("mtng.cli.get_keyring_token", lambda: "keyring-token")

    token, source = mtng.cli.resolve_github_token_with_source(None)
    assert token == "keyring-token"
    assert source == "system keychain"


def test_resolve_github_token_requires_token(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr("mtng.cli.get_keyring_token", lambda: None)

    with pytest.raises(typer.BadParameter):
        mtng.cli.resolve_github_token(None)


def test_login_stores_token(monkeypatch: pytest.MonkeyPatch):
    runner = CliRunner()
    stored = {}

    def fake_set_password(service: str, username: str, token: str):
        stored["service"] = service
        stored["username"] = username
        stored["token"] = token

    monkeypatch.setattr("mtng.cli.validate_github_token_sync", lambda token: "octocat")
    monkeypatch.setattr("mtng.cli.keyring.set_password", fake_set_password)

    result = runner.invoke(mtng.cli.cli, ["auth", "login", "--token", "abc123"])

    assert result.exit_code == 0
    assert stored == {
        "service": mtng.cli.KEYRING_SERVICE,
        "username": mtng.cli.KEYRING_USERNAME,
        "token": "abc123",
    }
    assert "Stored GitHub token for @octocat" in result.stdout


def test_login_without_validation(monkeypatch: pytest.MonkeyPatch):
    runner = CliRunner()
    stored = {}

    def fake_set_password(service: str, username: str, token: str):
        stored["service"] = service
        stored["username"] = username
        stored["token"] = token

    def fail_validation(_token: str):
        raise AssertionError("validation should be skipped")

    monkeypatch.setattr("mtng.cli.validate_github_token_sync", fail_validation)
    monkeypatch.setattr("mtng.cli.keyring.set_password", fake_set_password)

    result = runner.invoke(
        mtng.cli.cli,
        ["auth", "login", "--token", "abc123", "--no-validate"],
    )

    assert result.exit_code == 0
    assert stored["token"] == "abc123"
    assert "Stored GitHub token in your system keychain." in result.stdout


def test_auth_check(monkeypatch: pytest.MonkeyPatch):
    runner = CliRunner()

    monkeypatch.setattr("mtng.cli.resolve_github_token", lambda token: "resolved-token")
    monkeypatch.setattr("mtng.cli.validate_github_token_sync", lambda token: "octocat")

    result = runner.invoke(mtng.cli.cli, ["auth", "check"])

    assert result.exit_code == 0
    assert "GitHub token is valid for @octocat." in result.stdout


def test_format_github_request_error_with_status_code():
    err = DummyGitHubError(401, "Bad credentials")
    assert mtng.cli.format_github_request_error(err) == "401 Bad credentials"


def test_auth_status(monkeypatch: pytest.MonkeyPatch):
    runner = CliRunner()

    monkeypatch.setattr(
        "mtng.cli.resolve_github_token_with_source",
        lambda token: ("resolved-token", "GH_TOKEN"),
    )
    monkeypatch.setattr("mtng.cli.resolve_github_token", lambda token: "resolved-token")
    monkeypatch.setattr("mtng.cli.validate_github_token_sync", lambda token: "octocat")

    result = runner.invoke(mtng.cli.cli, ["auth", "status", "--validate"])

    assert result.exit_code == 0
    assert "GitHub token is configured (source: GH_TOKEN)." in result.stdout
    assert "GitHub token is valid for @octocat." in result.stdout
