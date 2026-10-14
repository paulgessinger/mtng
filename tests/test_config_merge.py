import json

import pytest
from typer.testing import CliRunner

from mtng import cli


@pytest.fixture
def config_layers(tmp_path):
    shared = tmp_path / "repos.toml"
    shared.write_text("""[repositories.acts]
name = "acts-project/acts"
display_name = "Shared"
include_labels = ["old"]
[repositories.other]
name = "other/repo"
""")
    meeting = tmp_path / "meeting.yml"
    meeting.write_text("""title: Weekly
repositories:
  acts:
    display_name: Meeting
    include_labels: []
sections:
  - type: changes
    state: open
    repositories: [acts]
""")
    return shared, meeting


def test_merge_partial_configs_and_validate_once(config_layers):
    spec = cli.load_spec(*config_layers)
    assert spec.title == "Weekly"
    assert spec.repositories["acts"].name == "acts-project/acts"
    assert spec.repositories["acts"].display_name == "Meeting"
    assert spec.repositories["acts"].include_labels == []
    assert spec.repositories["other"].name == "other/repo"
    assert spec.source_repositories == [spec.repositories["acts"]]


def test_later_lists_and_null_replace_earlier_values(config_layers, tmp_path):
    overlay = tmp_path / "override.json"
    overlay.write_text(json.dumps({"sections": [], "title": None}))
    spec = cli.load_spec(*config_layers, overlay)
    assert spec.sections == []
    assert spec.title is None
    assert len(spec.repositories) == 2


def test_legacy_lists_replace_without_mutating_input():
    base = {"repos": [{"name": "a/b"}]}
    assert cli.merge_config(base, {"repos": []}) == {"repos": []}
    assert base == {"repos": [{"name": "a/b"}]}


@pytest.mark.parametrize("content", ["[", "- not-a-mapping", ""])
def test_invalid_config_names_source(tmp_path, content):
    path = tmp_path / "broken.yml"
    path.write_text(content)
    with pytest.raises(ValueError, match="broken.yml"):
        cli.load_spec(path)


def test_unknown_alias_still_rejected(config_layers, tmp_path):
    path = tmp_path / "unknown.json"
    path.write_text(
        json.dumps({"sections": [{"type": "changes", "repositories": ["missing"]}]})
    )
    with pytest.raises(ValueError, match="Unknown repository aliases"):
        cli.load_spec(*config_layers, path)


@pytest.mark.parametrize("positional", [True, False])
def test_generate_and_render_multiple_configs(
    config_layers, tmp_path, monkeypatch, positional
):
    shared, meeting = config_layers
    captured = []

    async def collect(spec, *args):
        captured.append(spec)
        return {}

    monkeypatch.setattr(cli, "resolve_token", lambda *args, **kwargs: ("token", "test"))
    monkeypatch.setattr(cli, "collect_sections", collect)
    monkeypatch.setattr(cli, "generate_latex", lambda *args, **kwargs: "latex")
    snapshot = tmp_path / "data.json"
    args = [str(shared)] if positional else ["--config", str(shared)]
    result = CliRunner().invoke(
        cli.cli, ["generate", *args, "-c", str(meeting), "--save-data", str(snapshot)]
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == "latex"
    assert captured[-1].repositories["acts"].display_name == "Meeting"
    saved = json.loads(snapshot.read_text())
    assert saved["spec"]["repositories"]["acts"]["name"] == "acts-project/acts"
    result = CliRunner().invoke(
        cli.cli,
        ["render", str(snapshot), "--config", str(shared), "--config", str(meeting)],
    )
    assert result.exit_code == 0, result.output
    assert captured[-1] == captured[0]


def test_generate_requires_some_config():
    result = CliRunner().invoke(cli.cli, ["generate"])
    assert result.exit_code != 0
    assert "Provide a config argument" in result.output


def test_positional_stdin_config_still_supported(monkeypatch):
    monkeypatch.setattr(cli, "generate_latex", lambda *args, **kwargs: "latex")
    result = CliRunner().invoke(
        cli.cli, ["generate", "-", "--since", "2024-01-01"], input="repos: []\n"
    )
    assert result.exit_code == 0, result.output
    assert result.stdout == "latex"
