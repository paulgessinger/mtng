from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from mtng.forge.github import GitHubForge
from mtng.forge.gitlab import GitLabForge
from mtng.models import Issue
from mtng.predicates import filter_items, path_matches
from mtng.report_data import BackendData, RecordingForge, ReplayForge
from mtng.spec import Predicate, Spec
from test_sections import START, END, pr, spec_with
from fake_forge import FakeForge
from mtng.sections import collect_sections


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"any": []},
        {"all": []},
        {"labels_any": []},
        {"paths_any": [""]},
        {"paths_any": ["/src/**"]},
        {"labels_any": ["x"], "paths_any": ["src/**"]},
        {"unknown": ["x"]},
        {"not": {"any": []}},
    ],
)
def test_invalid_predicate(value):
    with pytest.raises(ValueError):
        Predicate.model_validate(value)


@pytest.mark.parametrize(
    "path,pattern,expected",
    [
        ("src/a.py", "src/**", True),
        ("src/deep/a.py", "src/*.py", False),
        ("src/a.py", "src/**/*.py", True),
        ("src/deep/a.py", "src/**/*.py", True),
        ("other/src/a.py", "src/**", False),
        ("SRC/a.py", "src/**", False),
        ("a.py", "**/*.py", True),
        ("src/a.py", "src/[ab].?", False),
        ("src/a.c", "src/[ab].?", True),
    ],
)
def test_globs(path, pattern, expected):
    assert path_matches(path, pattern) == expected


@pytest.mark.asyncio
async def test_nested_predicates_short_circuit_and_issue_semantics():
    predicate = Predicate.model_validate(
        {
            "all": [
                {"any": [{"labels_any": ["tracking"]}, {"paths_any": ["src/**"]}]},
                {"not": {"labels_any": ["backport"]}},
            ]
        }
    )
    assert Predicate.model_validate_json(predicate.model_dump_json()) == predicate
    items = [
        pr(number=1, labels=[{"name": "tracking"}]),
        pr(number=2, labels=[]),
        pr(number=3, labels=[{"name": "backport"}]),
    ]
    forge = SimpleNamespace(changed_paths=AsyncMock(return_value=["src/a.py"]))
    assert await filter_items(items, predicate, forge, "a/b") == items[:2]
    assert forge.changed_paths.await_count == 2
    issue = Issue.model_validate(items[0].model_dump())
    forge.changed_paths.reset_mock()
    assert await filter_items([issue], Predicate(paths_any=["**"]), forge, "a/b") == []
    forge.changed_paths.assert_not_awaited()
    assert await filter_items(items, None, forge, "a/b") is items


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "section",
    [
        {"type": "changes"},
        {"type": "statistics"},
        {"type": "changes", "release": "v1"},
        {"type": "changes", "state": "open"},
        {"type": "review_followup"},
    ],
)
async def test_collection_and_replay(section):
    items = [
        pr(number=1, labels=[{"name": "tracking"}]),
        pr(number=2, labels=[]),
        pr(number=3, labels=[]),
    ]
    backend = FakeForge(merged_prs=items, open_prs=items, release=("v1", items))
    backend.changed_paths = AsyncMock(
        side_effect=lambda repo, number: ["src/a.py"] if number == 2 else ["docs/a.md"]
    )
    spec = spec_with(
        dict(
            section,
            repositories=["acts"],
            where={
                "any": [
                    {"labels_any": ["tracking"]},
                    {"paths_any": ["src/**"]},
                ]
            },
        )
    )
    assert Spec.model_validate_json(spec.model_dump_json()) == spec
    recording = RecordingForge(backend, "github", "github.com")
    data = await collect_sections(
        spec, START, END, {("github", "github.com"): recording}
    )
    block = data["0:acts"]
    if section["type"] != "review_followup":
        key = "open_prs" if section.get("state") == "open" else "merged_prs"
        assert [item.number for item in block[key]] == [1, 2]
        if key == "merged_prs":
            assert block["release_stats"].pr_count == 2
    else:
        assert {
            item.number for group in block["review_followup"] for item in group["items"]
        } == {1, 2}
    saved = BackendData.model_validate_json(recording.data.model_dump_json())
    replayed = await collect_sections(
        spec, START, END, {("github", "github.com"): ReplayForge(saved)}
    )
    assert replayed == data
    with pytest.raises(ValueError, match="Collect a new snapshot"):
        await ReplayForge(saved).changed_paths("acts-project/acts", 99)


@pytest.mark.asyncio
async def test_github_paths_and_limit():
    async def files(url):
        assert url == "/repos/a/b/pulls/1/files?per_page=100"
        yield {"filename": "new/a.py", "previous_filename": "old/a.py"}
        yield {"filename": "deleted.py"}

    forge = GitHubForge(SimpleNamespace(getiter=files))
    assert await forge.changed_paths("a/b", 1) == ["deleted.py", "new/a.py", "old/a.py"]

    async def truncated(url):
        for _ in range(3000):
            yield {"filename": "a.py"}

    forge.gh.getiter = truncated
    with pytest.raises(ValueError, match="truncated"):
        await forge.changed_paths("a/b", 1)


@pytest.mark.asyncio
async def test_gitlab_paths_and_overflow():
    forge = GitLabForge(None, "gitlab.example", "test")
    forge.getitem = AsyncMock(
        return_value={
            "changes": [
                {"old_path": "old/a.py", "new_path": "new/a.py"},
                {"old_path": "deleted.py", "new_path": "deleted.py"},
            ],
            "overflow": False,
        }
    )
    assert await forge.changed_paths("group/sub/repo", 1) == [
        "deleted.py",
        "new/a.py",
        "old/a.py",
    ]
    forge.getitem.assert_awaited_once_with(
        "/projects/group%2Fsub%2Frepo/merge_requests/1/changes"
    )
    forge.getitem.return_value["overflow"] = True
    with pytest.raises(ValueError, match="truncated"):
        await forge.changed_paths("group/sub/repo", 1)


def test_old_snapshot_defaults():
    assert BackendData(forge="github", host="github.com").changed_paths == []


@pytest.mark.asyncio
async def test_existing_label_filters_remain_and_constraints():
    kept = pr(number=1, labels=[{"name": "required"}])

    class LabelForge(FakeForge):
        async def merged_prs(
            self, repo_name, start, end, with_labels=[], without_labels=[]
        ):
            assert with_labels == ["required"]
            assert without_labels == ["backport"]
            return [kept]

    forge = LabelForge()
    forge.changed_paths = AsyncMock(return_value=["src/a.py"])
    spec = spec_with(
        {
            "type": "changes",
            "repositories": ["acts"],
            "labels": {"include": ["required"], "exclude": ["backport"]},
            "where": {"any": [{"labels_any": ["tracking"]}, {"paths_any": ["src/**"]}]},
        }
    )
    block = (
        await collect_sections(spec, START, END, {("github", "github.com"): forge})
    )["0:acts"]
    assert block["merged_prs"] == [kept]
    assert block["release_stats"].pr_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,key",
    [
        ("issues", "recent_issues"),
        ("stale", "stale"),
        ("discussion", "needs_discussion"),
    ],
)
async def test_issue_sections(kind, key):
    items = [
        Issue.model_validate(pr(number=n, labels=[{"name": label}]).model_dump())
        for n, label in [(1, "tracking"), (2, "other")]
    ]
    forge = FakeForge(recent_issues=items, labelled={"flag": items})
    section = {
        "type": kind,
        "repositories": ["acts"],
        "where": {"labels_all": ["tracking"]},
    }
    if kind != "issues":
        section["label"] = "flag"
    block = (
        await collect_sections(
            spec_with(section), START, END, {("github", "github.com"): forge}
        )
    )["0:acts"]
    assert [item.number for item in block[key]] == [1]
