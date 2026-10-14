import json
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

import mtng.cli
from mtng.generate import generate_latex
from mtng.models import PullRequest
from mtng.sections import collect_sections
from mtng.spec import Spec
from fake_forge import FakeForge

START = datetime(2022, 8, 1, tzinfo=timezone.utc)
END = datetime(2022, 8, 17, tzinfo=timezone.utc)


def pr(**changes):
    raw = json.loads((Path(__file__).parent / "ref/merged_prs.json").read_text())[0]
    raw.update(changes)
    return PullRequest.model_validate(raw)


def spec_with(*sections):
    return Spec.model_validate(
        {
            "repositories": {"acts": {"name": "acts-project/acts"}},
            "sections": list(sections),
        }
    )


def test_toml_sections_roundtrip():
    spec = Spec.model_validate(tomllib.loads("""
title = "Team update"
[repositories.acts]
name = "acts-project/acts"
[[sections]]
type = "changes"
repositories = ["acts"]
state = "open"
drafts = "only"
[sections.labels]
include = ["tracking"]
exclude = ["backport"]
[[sections]]
type = "indico"
event = "https://indico.example/event/123/"
"""))
    assert spec.sections[0].labels.include == ["tracking"]
    assert spec.sections[1].type == "indico"
    assert Spec.model_validate_json(spec.model_dump_json()) == spec


@pytest.mark.parametrize(
    "config",
    [
        {"repos": [{"name": "a/b"}], "sections": []},
        {"repositories": {"a": {"name": "a/b"}}},
        {"sections": [{"type": "changes", "repositories": ["missing"]}]},
        {"sections": [{"type": "indico", "event": "not-an-event"}]},
        {"sections": [{"type": "unknown"}]},
    ],
)
def test_invalid_sections(config):
    with pytest.raises(ValueError):
        Spec.model_validate(config)


@pytest.mark.asyncio
async def test_ordered_sections_and_native_drafts():
    spec = spec_with(
        {
            "type": "changes",
            "repositories": ["acts"],
            "state": "open",
            "drafts": "only",
            "title": "Draft work",
        },
        {
            "type": "indico",
            "event": "https://indico.example/event/123/",
            "title": "Meeting agenda",
        },
        {
            "type": "changes",
            "repositories": ["acts"],
            "title": "Landed work",
            "summary": False,
        },
    )
    forge = FakeForge(
        open_prs=[
            pr(title="Draft item", draft=True),
            pr(title="Ready item", draft=False),
        ],
        merged_prs=[pr(title="Merged item")],
    )
    data = await collect_sections(spec, START, END, forge)
    assert [item.title for item in data["0:acts"]["open_prs"]] == ["Draft item"]
    events = {
        spec.sections[1].event: [
            {
                "title": "Agenda item",
                "speakers": [],
                "url": "https://indico.example/contribution/1",
            }
        ]
    }
    output = generate_latex(spec, data, START, END, [], False, events)
    assert (
        output.index("Draft work")
        < output.index("Meeting agenda")
        < output.index("Landed work")
    )
    assert "Ready item" not in output
    assert "No merged" not in output
    assert "period at a glance" not in output
    assert "acts-project/acts" in output
    assert "0:acts" not in output


@pytest.mark.asyncio
async def test_release_does_not_suppress_other_sections():
    spec = spec_with(
        {"type": "changes", "repositories": ["acts"], "release": "v1"},
        {"type": "changes", "repositories": ["acts"], "state": "open"},
    )
    forge = FakeForge(release=("v1", [pr()]), open_prs=[pr(draft=False)])
    data = await collect_sections(spec, START, END, forge)
    assert forge.calls == ["release_prs", "open_prs"]
    assert data["0:acts"]["release_tag"] == "v1"
    assert len(data["1:acts"]["open_prs"]) == 1
    assert not spec.needs_since(None)


@pytest.mark.asyncio
async def test_section_filters_reach_forge():
    spec = spec_with(
        {
            "type": "changes",
            "state": "open",
            "repositories": ["acts"],
            "labels": {"include": ["tracking"], "exclude": ["backport"]},
        }
    )
    forge = FakeForge()
    forge.open_prs = AsyncMock(return_value=[])
    await collect_sections(spec, START, END, forge)
    assert forge.open_prs.call_args.kwargs["with_labels"] == ["tracking"]
    assert forge.open_prs.call_args.kwargs["without_labels"] == ["backport"]


def test_indico_only_save_and_offline_render(monkeypatch, tmp_path):
    config = tmp_path / "report.toml"
    config.write_text("""[[sections]]
type = "indico"
event = "https://indico.example/event/123/"
""")
    monkeypatch.setattr(
        mtng.cli,
        "handle_event",
        AsyncMock(
            return_value=[
                {
                    "title": "Agenda item",
                    "speakers": ["Alice"],
                    "start_date": START,
                    "url": "https://indico.example/contribution/1",
                }
            ]
        ),
    )
    snapshot, first, second = [
        tmp_path / name for name in ("report.json", "first.tex", "second.tex")
    ]
    runner = CliRunner()
    result = runner.invoke(
        mtng.cli.cli,
        ["generate", str(config), "--save-data", str(snapshot), "--tex", str(first)],
    )
    assert result.exit_code == 0, result.output

    def fail(*args, **kwargs):
        raise AssertionError("offline render must not access network or credentials")

    monkeypatch.setattr(mtng.cli.aiohttp, "ClientSession", fail)
    monkeypatch.setattr(mtng.cli, "resolve_token", fail)
    result = runner.invoke(
        mtng.cli.cli, ["render", str(snapshot), "--tex", str(second)]
    )
    assert result.exit_code == 0, result.output
    assert first.read_text() == second.read_text()
    assert "Agenda item" in second.read_text()


@pytest.mark.asyncio
async def test_full_deck_uses_names_not_internal_section_keys():
    spec = spec_with(
        {"type": "statistics", "repositories": ["acts"]},
        {"type": "changes", "repositories": ["acts"], "summary": False},
    )
    data = await collect_sections(spec, START, END, FakeForge(merged_prs=[pr()]))
    output = generate_latex(spec, data, START, END, [], True)
    assert "0:acts" not in output and "1:acts" not in output
    assert "acts-project/acts" in output
    assert output.count("period at a glance") == 1


@pytest.mark.parametrize(
    "drafts, expected",
    [
        ("exclude", ["Ready"]),
        ("only", ["Draft", "WIP"]),
        ("include", ["Ready", "Draft", "WIP"]),
    ],
)
@pytest.mark.asyncio
async def test_draft_selection_includes_label_state(drafts, expected):
    spec = spec_with(
        {"type": "changes", "state": "open", "repositories": ["acts"], "drafts": drafts}
    )
    spec.repositories["acts"].wip_label = "WIP"
    forge = FakeForge(
        open_prs=[
            pr(title="Ready", draft=False, labels=[]),
            pr(title="Draft", draft=True, labels=[]),
            pr(title="WIP", draft=False, labels=[{"name": "WIP", "color": "000000"}]),
        ]
    )
    data = await collect_sections(spec, START, END, forge)
    assert [item.title for item in data["0:acts"]["open_prs"]] == expected


@pytest.mark.asyncio
async def test_other_section_types_do_not_emit_merged_placeholder():
    from mtng.models import Issue

    issue = Issue.model_validate(pr(title="Discuss this").model_dump())
    spec = spec_with(
        {
            "type": "discussion",
            "repositories": ["acts"],
            "label": "discuss",
            "title": "Discussion",
        },
        {"type": "issues", "repositories": ["acts"], "title": "Recent issues"},
        {
            "type": "stale",
            "repositories": ["acts"],
            "label": "stale",
            "title": "Stale work",
        },
    )
    forge = FakeForge(
        recent_issues=[issue], labelled={"discuss": [issue], "stale": [issue]}
    )
    data = await collect_sections(spec, START, END, forge)
    output = generate_latex(spec, data, START, END, [], False)
    assert (
        output.index("Discussion")
        < output.index("Recent issues")
        < output.index("Stale work")
    )
    assert "No merged" not in output
    assert forge.calls == ["open_issues", "open_issues", "open_issues"]


def test_section_snapshot_reorder_and_missing_event(monkeypatch, tmp_path):
    config = tmp_path / "report.toml"
    config.write_text("""[repositories.acts]
name = "acts-project/acts"
[[sections]]
type = "changes"
repositories = ["acts"]
state = "open"
title = "Open work"
[[sections]]
type = "changes"
repositories = ["acts"]
title = "Merged work"
""")
    monkeypatch.setattr(
        mtng.cli, "resolve_token", lambda *a, **kw: ("secret", "--token")
    )
    monkeypatch.setattr(
        mtng.cli,
        "GitHubForge",
        lambda *a: FakeForge(open_prs=[pr(draft=False)], merged_prs=[pr()]),
    )
    snapshot = tmp_path / "report.json"
    runner = CliRunner()
    result = runner.invoke(
        mtng.cli.cli,
        [
            "generate",
            str(config),
            "--since",
            START.isoformat(),
            "--save-data",
            str(snapshot),
        ],
    )
    assert result.exit_code == 0, result.output
    override = Spec.model_validate(tomllib.loads(config.read_text()))
    override.sections.reverse()
    override_path = tmp_path / "override.json"
    override_path.write_text(override.model_dump_json())

    def fail(*args, **kwargs):
        raise AssertionError("offline rendering must not access the network")

    monkeypatch.setattr(mtng.cli.aiohttp, "ClientSession", fail)
    tex = tmp_path / "out.tex"
    result = runner.invoke(
        mtng.cli.cli,
        ["render", str(snapshot), "--config", str(override_path), "--tex", str(tex)],
    )
    assert result.exit_code == 0, result.output
    output = tex.read_text()
    assert output.index("Merged work") < output.index("Open work")
    override_path.write_text(
        json.dumps(
            {
                "sections": [
                    {"type": "indico", "event": "https://indico.example/event/999/"}
                ]
            }
        )
    )
    result = runner.invoke(
        mtng.cli.cli, ["render", str(snapshot), "--config", str(override_path)]
    )
    assert result.exit_code != 0 and "collect a new snapshot" in result.output


def test_action_rejects_gitlab_in_explicit_config(tmp_path):
    from mtng.action import resolve_config, ActionInputError

    path = tmp_path / "report.toml"
    path.write_text("""[repositories.lab]
name = "group/project"
forge = "gitlab"
[[sections]]
type = "changes"
repositories = ["lab"]
""")
    with pytest.raises(ActionInputError, match="only supports GitHub"):
        resolve_config({"MTNG_INPUT_CONFIG": str(path)}, tmp_path / "generated.json")


def test_old_snapshot_without_section_fields_loads():
    from mtng.report_data import ReportData

    saved = ReportData.model_validate(
        {
            "version": 1,
            "spec": {"repos": [{"name": "acts-project/acts"}]},
            "since": START.isoformat(),
            "now": END.isoformat(),
            "backends": [],
        }
    )
    assert saved.spec.sections is None
    assert saved.events == {}
    assert saved.spec.source_repositories == saved.spec.repos


@pytest.mark.asyncio
@pytest.mark.parametrize("full", [True, False])
async def test_compile_section_composition(tmp_path, full):
    import shutil
    import subprocess

    latexmk = shutil.which("latexmk")
    if latexmk is None:
        pytest.skip("latexmk not found")
    spec = spec_with(
        {"type": "statistics", "repositories": ["acts"]},
        {
            "type": "changes",
            "repositories": ["acts"],
            "group_by": "category",
            "summary": False,
        },
        {
            "type": "changes",
            "repositories": ["acts"],
            "state": "open",
            "drafts": "include",
        },
        {"type": "indico", "event": "https://indico.example/event/123/"},
    )
    data = await collect_sections(
        spec,
        START,
        END,
        FakeForge(
            merged_prs=[pr(title="feat!: Breaking change")],
            open_prs=[pr(title="Draft work", draft=True)],
        ),
    )
    events = {
        spec.sections[-1].event: [
            {"title": "Agenda", "speakers": ["Alice"], "url": "https://indico.example"}
        ]
    }
    output = generate_latex(spec, data, START, END, [], full, events)
    if not full:
        output = (
            "\\documentclass{beamer}\n\\begin{document}\n"
            + output
            + "\n\\end{document}"
        )
    source = tmp_path / "report.tex"
    source.write_text(output)
    result = subprocess.run(
        [
            latexmk,
            "-pdf",
            "-halt-on-error",
            f"-output-directory={tmp_path}",
            str(source),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert result.returncode == 0, result.stdout
    assert (tmp_path / "report.pdf").exists()
