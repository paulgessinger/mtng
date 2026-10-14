import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from typer.testing import CliRunner

import mtng.cli
from mtng.collect import collect_repositories
from mtng.forge.base import FetchRequirements
from mtng.forge.github import GitHubForge
from mtng.forge.gitlab import GitLabForge
from mtng.generate import generate_latex
from mtng.models import PullRequest
from mtng.report_data import RecordingForge, ReportData
from mtng.spec import Repository, Spec
from fake_forge import FakeForge
from test_forge_github import client
from test_forge_gitlab import payload

START = datetime(2022, 8, 1, tzinfo=timezone.utc)
END = datetime(2022, 8, 17, tzinfo=timezone.utc)


def pr_fixture():
    return PullRequest.model_validate(
        json.loads((Path(__file__).parent / "ref" / "merged_prs.json").read_text())[0]
    )


@pytest.mark.asyncio
async def test_snapshot_roundtrip_and_repeated_rendering():
    repo = Repository(name="acts-project/acts", show_wip=True)
    spec = Spec(repos=[repo])
    recorder = RecordingForge(
        FakeForge(merged_prs=[pr_fixture()], open_prs=[pr_fixture()]), *repo.forge_key
    )
    original = await collect_repositories([repo], START, END, recorder)
    saved = ReportData(spec=spec, since=START, now=END, backends=[recorder.data])
    saved = ReportData.model_validate_json(saved.model_dump_json())
    expected = generate_latex(spec, original, START, END, [], False)
    for _ in range(2):
        replayed = await collect_repositories([repo], START, END, saved.forges())
        assert generate_latex(spec, replayed, START, END, [], False) == expected


@pytest.mark.asyncio
async def test_release_snapshot_preserves_tag_and_item_types():
    repo = Repository(name="a/b", forge="gitlab")
    recorder = RecordingForge(
        FakeForge(release=("v1/rc", [pr_fixture()])), *repo.forge_key
    )
    await collect_repositories([repo], START, END, recorder, release="v1/rc")
    saved = ReportData(
        spec=Spec(repos=[repo]),
        since=START,
        now=END,
        release="v1/rc",
        backends=[recorder.data],
    )
    loaded = ReportData.model_validate_json(saved.model_dump_json())
    result = await collect_repositories(
        [repo], START, END, loaded.forges(), release=loaded.release
    )
    assert result[repo.name]["release_tag"] == "v1/rc"
    assert isinstance(result[repo.name]["merged_prs"][0], PullRequest)


@pytest.mark.asyncio
async def test_open_prs_skip_unneeded_details_and_reviews():
    gh = client()
    raw = pr_fixture().model_dump(mode="json")
    raw["pull_request"] = {"url": raw["url"]}
    gh._make_request = AsyncMock(return_value=({"items": [raw]}, None, 200))
    prs = await GitHubForge(gh).open_prs(
        "acts-project/acts",
        requirements=FetchRequirements(reviews=False, details=False),
    )
    assert len(prs) == 1 and not prs[0].reviews_available
    assert prs[0].url == raw["url"]
    assert gh._make_request.await_count == 1


@pytest.mark.asyncio
async def test_gitlab_skips_unneeded_approvals():
    forge = GitLabForge(None, "gitlab.example", "unused")
    forge.request = AsyncMock(return_value=([payload()], {"X-Next-Page": ""}))
    prs = await forge.open_prs(
        "a/b", requirements=FetchRequirements(reviews=False, details=False)
    )
    assert len(prs) == 1 and not prs[0].reviews_available
    assert forge.request.await_count == 1


def test_generate_save_and_render_without_credentials_or_session(monkeypatch, tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        Spec(
            repos=[Repository(name="acts-project/acts", show_wip=True)]
        ).model_dump_json()
    )
    snapshot, first, second = [
        tmp_path / name for name in ("report.json", "first.tex", "second.tex")
    ]
    monkeypatch.setattr(
        mtng.cli,
        "resolve_token",
        lambda token, target, **kwargs: ("must-not-persist", "--token"),
    )
    monkeypatch.setattr(
        mtng.cli,
        "GitHubForge",
        lambda gh: FakeForge(merged_prs=[pr_fixture()], open_prs=[pr_fixture()]),
    )
    runner = CliRunner()
    result = runner.invoke(
        mtng.cli.cli,
        [
            "generate",
            str(config),
            "--since",
            "2022-08-01",
            "--now",
            "2022-08-17",
            "--save-data",
            str(snapshot),
            "--tex",
            str(first),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "must-not-persist" not in snapshot.read_text()

    def fail(*args, **kwargs):
        raise AssertionError(
            "Offline render must not resolve credentials or create a session"
        )

    monkeypatch.setattr(mtng.cli, "resolve_token", fail)
    monkeypatch.setattr(mtng.cli.aiohttp, "ClientSession", fail)
    result = runner.invoke(
        mtng.cli.cli, ["render", str(snapshot), "--tex", str(second)]
    )
    assert result.exit_code == 0, result.output
    assert first.read_text() == second.read_text()
    changed = Spec.model_validate_json(config.read_text())
    changed.title = "Offline presentation"
    changed.repos[0].display_name = "Renamed"
    config.write_text(changed.model_dump_json())
    result = runner.invoke(
        mtng.cli.cli,
        [
            "render",
            str(snapshot),
            "--config",
            str(config),
            "--full",
            "--tex",
            str(second),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (
        "Offline presentation" in second.read_text() and "Renamed" in second.read_text()
    )

    changed.repos[0].do_reviewers = True
    config.write_text(changed.model_dump_json())
    result = runner.invoke(
        mtng.cli.cli, ["render", str(snapshot), "--config", str(config)]
    )
    assert result.exit_code != 0 and "Collect a new snapshot" in result.output


def test_unknown_snapshot_version_rejected(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text('{"version": 99}')
    result = CliRunner().invoke(mtng.cli.cli, ["render", str(path)])
    assert result.exit_code != 0 and "version" in result.output
