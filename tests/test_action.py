from pathlib import Path

import pytest
import yaml

from mtng.action import (
    ActionInputError,
    SPEC_FIELDS,
    REPOSITORY_FIELDS,
    build_spec,
    env_name,
    main,
    resolve_config,
)
from mtng.cli import load_spec

ACTION_YML = Path(__file__).parent.parent / "action.yml"


def inputs(**kwargs):
    return {env_name(name): value for name, value in kwargs.items()}


def test_build_spec_from_inputs():
    spec = build_spec(
        inputs(
            repository="acts-project/acts",
            title="ACTS {release}",
            stale_label="Stale",
            show_wip="true",
            do_recent_issues="yes",
            no_assignee_attention="false",
            filter_labels="backport\nStale",
            pr_category_labels="feat=Feature, fix=Bugfix",
            pr_category_order="fix\nfeat",
        )
    )

    assert spec.title == "ACTS {release}"
    (repo,) = spec.repos
    assert repo.name == "acts-project/acts"
    assert repo.stale_label == "Stale"
    assert repo.show_wip is True
    assert repo.do_recent_issues is True
    assert repo.no_assignee_attention is False
    assert repo.filter_labels == ["backport", "Stale"]
    assert repo.pr_category_labels == {"feat": "Feature", "fix": "Bugfix"}
    assert repo.pr_category_order == ["fix", "feat"]


def test_build_spec_keeps_defaults_for_unset_inputs():
    spec = build_spec(inputs(repository="acts-project/acts"))

    (repo,) = spec.repos
    assert spec.title is None
    assert repo.show_wip is False
    assert repo.do_open_prs is True
    assert repo.stale_label is None


def test_build_spec_falls_back_to_current_repository():
    spec = build_spec({"GITHUB_REPOSITORY": "acts-project/acts"})

    assert spec.repos[0].name == "acts-project/acts"


def test_build_spec_without_repository():
    with pytest.raises(ActionInputError, match="No repository"):
        build_spec({})


def test_build_spec_rejects_invalid_boolean():
    with pytest.raises(ActionInputError, match="show-wip"):
        build_spec(inputs(repository="a/b", show_wip="maybe"))


def test_build_spec_rejects_invalid_mapping():
    with pytest.raises(ActionInputError, match="pr-category-labels"):
        build_spec(inputs(repository="a/b", pr_category_labels="feat"))


def test_build_spec_surfaces_model_validation_errors():
    with pytest.raises(ActionInputError, match="mutually exclusive"):
        build_spec(
            inputs(repository="a/b", filter_labels="backport", include_labels="core")
        )


def test_resolve_config_writes_loadable_spec(tmp_path):
    generated = tmp_path / "spec.json"

    path = resolve_config(inputs(repository="acts-project/acts"), generated)

    assert path == generated
    with path.open() as fh:
        spec = load_spec(fh)
    assert spec.repos[0].name == "acts-project/acts"


def test_resolve_config_passes_through_config_file(tmp_path):
    config = tmp_path / "spec.toml"
    config.write_text('[[repos]]\nname = "acts-project/acts"\n')

    path = resolve_config(inputs(config=str(config)), tmp_path / "generated.json")

    assert path == config


def test_resolve_config_rejects_config_with_spec_inputs(tmp_path):
    config = tmp_path / "spec.toml"
    config.write_text('[[repos]]\nname = "acts-project/acts"\n')

    with pytest.raises(ActionInputError, match="stale-label"):
        resolve_config(
            inputs(config=str(config), stale_label="Stale"),
            tmp_path / "generated.json",
        )


def test_resolve_config_rejects_missing_config_file(tmp_path):
    with pytest.raises(ActionInputError, match="does not exist"):
        resolve_config(
            inputs(config=str(tmp_path / "nope.toml")), tmp_path / "generated.json"
        )


def test_main_prints_config_path(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(env_name("repository"), "acts-project/acts")
    generated = tmp_path / "spec.json"

    assert main([str(generated)]) == 0

    assert capsys.readouterr().out.strip() == str(generated)
    assert generated.is_file()


def test_main_reports_input_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    monkeypatch.setenv(env_name("show_wip"), "maybe")
    monkeypatch.setenv(env_name("repository"), "acts-project/acts")

    assert main([str(tmp_path / "spec.json")]) == 2

    assert "::error::" in capsys.readouterr().err


def test_action_exposes_every_spec_field():
    """The action passes inputs through by name, so a new spec field needs a
    matching input declaration to be reachable from a workflow."""
    action = yaml.safe_load(ACTION_YML.read_text())

    declared = set(action["inputs"])
    assert set(SPEC_FIELDS) <= declared
    assert set(REPOSITORY_FIELDS) <= declared


def test_action_maps_every_input_to_an_environment_variable():
    action = yaml.safe_load(ACTION_YML.read_text())

    (step,) = [s for s in action["runs"]["steps"] if s.get("id") == "config"]
    for field in ["config", "repository"] + SPEC_FIELDS + REPOSITORY_FIELDS:
        assert env_name(field) in step["env"]
