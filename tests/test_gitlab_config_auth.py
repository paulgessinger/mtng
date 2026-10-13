from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

import mtng.cli
from mtng.auth import resolve_gitlab_token
from mtng.spec import Repository, Spec


def test_forge_defaults_and_nested_paths():
    assert Repository(name="owner/repo").forge_key == ("github", "github.com")
    assert Repository(name="group/sub/project", forge="gitlab").forge_key == (
        "gitlab",
        "gitlab.com",
    )
    repo = Repository(
        name="group/project", forge="gitlab", host="https://GITLAB.CERN.CH:443/"
    )
    assert repo.forge_key == ("gitlab", "gitlab.cern.ch")


@pytest.mark.parametrize(
    "host",
    [
        "http://gitlab.com",
        "user:password@gitlab.com",
        "gitlab.com/api",
        "gitlab.com?token=x",
        "gitlab.com#x",
        "gitlab.com:99999",
        "",
    ],
)
def test_invalid_hosts(host):
    with pytest.raises(ValueError):
        Repository(name="group/project", forge="gitlab", host=host)


def test_duplicate_display_keys_and_unsupported_github_hosts():
    with pytest.raises(ValueError, match="Duplicate"):
        Spec(repos=[Repository(name="a/b"), Repository(name="a/b", forge="gitlab")])
    with pytest.raises(ValueError, match="Enterprise"):
        Repository(name="a/b", host="github.example")


def test_gitlab_credential_precedence_and_host_scoping(monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "env-token")
    monkeypatch.setenv("GH_TOKEN", "never-use-for-gitlab")
    requested = []

    def get(service, username):
        requested.append((service, username))
        return "host-token"

    monkeypatch.setattr("mtng.auth.keyring.get_password", get)
    assert resolve_gitlab_token(" explicit ", "gitlab.cern.ch") == (
        "explicit",
        "--token",
    )
    assert resolve_gitlab_token(None, "gitlab.cern.ch") == ("env-token", "GITLAB_TOKEN")
    assert resolve_gitlab_token(None, "gitlab.cern.ch", allow_env=False) == (
        "host-token",
        "system keychain",
    )
    assert requested == [("mtng", "gitlab:gitlab.cern.ch:token")]


def test_gitlab_login_check_status(monkeypatch):
    stored = {}
    monkeypatch.setattr(
        "mtng.cli.keyring.set_password",
        lambda service, user, token: stored.update(
            service=service, user=user, token=token
        ),
    )
    validate = AsyncMock(return_value="alice")
    monkeypatch.setattr("mtng.cli.validate_gitlab_token", validate)
    runner = CliRunner()
    result = runner.invoke(
        mtng.cli.cli, ["auth", "login", "--host", "gitlab.cern.ch", "--token", "secret"]
    )
    assert result.exit_code == 0, result.output
    assert stored == {
        "service": "mtng",
        "user": "gitlab:gitlab.cern.ch:token",
        "token": "secret",
    }
    for command in ("check", "status"):
        result = runner.invoke(
            mtng.cli.cli,
            ["auth", command, "--host", "gitlab.cern.ch", "--token", "secret"],
        )
        assert result.exit_code == 0, result.output
        assert "secret" not in result.output


def write_config(tmp_path, repos):
    path = tmp_path / "spec.json"
    path.write_text(Spec(repos=repos).model_dump_json())
    return path


def test_generate_routes_tokens_to_correct_hosts(monkeypatch, tmp_path):
    path = write_config(
        tmp_path,
        [
            Repository(name="a/b"),
            Repository(name="a/c", forge="gitlab", host="gitlab.cern.ch"),
        ],
    )
    monkeypatch.setenv("GH_TOKEN", "github-secret")
    monkeypatch.setenv("GITLAB_TOKEN", "gitlab-secret")
    captured = {}

    async def collect(repos, **kwargs):
        captured.update(kwargs["forge"])
        return {}

    monkeypatch.setattr(mtng.cli, "collect_repositories", collect)
    result = CliRunner().invoke(
        mtng.cli.cli, ["generate", str(path), "--since", "2026-01-01"]
    )
    assert result.exit_code == 0, result.output
    assert captured[("github", "github.com")].gh.oauth_token == "github-secret"
    assert captured[("gitlab", "gitlab.cern.ch")].token == "gitlab-secret"


@pytest.mark.parametrize("explicit", [True, False])
def test_ambiguous_tokens_fail_before_requests(monkeypatch, tmp_path, explicit):
    path = write_config(
        tmp_path,
        [
            Repository(name="a/b", forge="gitlab"),
            Repository(name="a/c", forge="gitlab", host="gitlab.cern.ch"),
        ],
    )
    monkeypatch.setenv("GITLAB_TOKEN", "ambiguous")
    collect = AsyncMock()
    monkeypatch.setattr(mtng.cli, "collect_repositories", collect)
    args = ["generate", str(path), "--since", "2026-01-01"]
    if explicit:
        args += ["--token", "ambiguous"]
    result = CliRunner().invoke(mtng.cli.cli, args)
    assert result.exit_code != 0
    collect.assert_not_called()


def test_multiple_gitlab_hosts_use_independent_keyring_entries(monkeypatch, tmp_path):
    path = write_config(
        tmp_path,
        [
            Repository(name="a/b", forge="gitlab"),
            Repository(name="a/c", forge="gitlab", host="gitlab.cern.ch"),
        ],
    )
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    monkeypatch.setattr(
        "mtng.auth.keyring.get_password",
        lambda service, user: {
            "gitlab:gitlab.com:token": "cloud",
            "gitlab:gitlab.cern.ch:token": "cern",
        }[user],
    )
    captured = {}

    async def collect(repos, **kwargs):
        captured.update(kwargs["forge"])
        return {}

    monkeypatch.setattr(mtng.cli, "collect_repositories", collect)
    result = CliRunner().invoke(
        mtng.cli.cli, ["generate", str(path), "--since", "2026-01-01"]
    )
    assert result.exit_code == 0, result.output
    assert captured[("gitlab", "gitlab.com")].token == "cloud"
    assert captured[("gitlab", "gitlab.cern.ch")].token == "cern"


def test_gitlab_frame_defaults_roundtrip_and_explicit_overrides():
    repo = Repository(name="a/b", forge="gitlab")
    assert "MRs" in repo.frame_titles["open_prs"]
    assert (
        "MRs"
        in Repository.model_validate_json(repo.model_dump_json()).frame_titles[
            "open_prs"
        ]
    )
    repo = Repository(name="a/b", forge="gitlab", frame_titles={"open_prs": "My PRs"})
    assert repo.frame_titles["open_prs"] == "My PRs"


def test_generate_reports_api_failure_without_token(monkeypatch, tmp_path):
    from mtng.forge.gitlab import GitLabError

    path = write_config(tmp_path, [Repository(name="a/b", forge="gitlab")])
    monkeypatch.setenv("GITLAB_TOKEN", "secret-value")
    monkeypatch.setattr(
        mtng.cli,
        "collect_repositories",
        AsyncMock(side_effect=GitLabError(401, "/projects/a%2Fb")),
    )
    result = CliRunner().invoke(
        mtng.cli.cli, ["generate", str(path), "--since", "2026-01-01"]
    )
    assert result.exit_code != 0
    assert "401" in result.output and "secret-value" not in result.output
