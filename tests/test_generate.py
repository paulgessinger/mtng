from asyncio.subprocess import STDOUT
import itertools
from re import sub
import shutil
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import os
import subprocess

import pytest
import pydantic
import aiohttp
from gidgethub.aiohttp import GitHubAPI
import yaml
from dateutil.tz import tzlocal

import mtng.collect
from mtng.generate import (
    build_placeholders,
    env,
    expand_placeholders,
    generate_latex,
)
from mtng.spec import Repository, Spec
from mtng.models import Label, PullRequest, Issue, Review, User
from mtng.collect import (
    collect_repositories,
    parse_pr_category,
    parse_pr_metadata,
    resolve_category_label,
)
from mtng.forge import GitHubForge
from mtng.forge.github import extract_release_pull_numbers, parse_release_tag

from fake_forge import FakeForge


@pytest.mark.asyncio
async def test_generate(tmp_path):
    repo = Repository(
        name="acts-project/acts",
        stale_label="Stale",
        wip_label=":construction: WIP",
    )

    ref = Path(__file__).parent / "ref"

    def get_file_content(file: str, cls):
        with (ref / file).open() as fh:
            return [cls.model_validate(o) for o in json.load(fh)]

    forge = FakeForge(
        merged_prs=get_file_content("merged_prs.json", PullRequest),
        open_prs=get_file_content("open_prs.json", Issue),
        recent_issues=get_file_content("recent_issues.json", Issue),
        labelled={"Stale": get_file_content("stale.json", Issue)},
    )
    since = datetime(2022, 8, 1, tzinfo=tzlocal())
    now = datetime(2022, 8, 11, tzinfo=tzlocal())
    result = await mtng.collect.collect_repositories(
        [repo], since=since, now=now, forge=forge
    )

    output = generate_latex(
        Spec(repos=[repo]),
        result,
        since=since,
        now=now,
        contributions=[],
        full_tex=False,
    )

    output += "\n"  # newline at end of file

    act_file = tmp_path / "output.tex"
    act_file.write_text(output)

    ref_file = ref / "reference.tex"

    if os.environ.get("UPDATE_SNAPSHOTS"):
        ref_file.write_text(output)
    else:
        assert output == ref_file.read_text(), (
            f"Output differs from snapshot. Run with UPDATE_SNAPSHOTS=1 to regenerate.\n"
            f"Actual output written to: {act_file}"
        )


@pytest.mark.parametrize("sections", [None, []])
@pytest.mark.parametrize(
    "config,expected",
    [
        ({}, "22529E"),
        ({"accent_color": "#7c3aEd"}, "7C3AED"),
        ({"accent_color": "008080"}, "008080"),
    ],
)
def test_full_tex_uses_accent_color(sections, config, expected):
    now = datetime(2022, 8, 11, tzinfo=tzlocal())
    output = generate_latex(
        Spec(sections=sections, **config),
        {},
        since=now,
        now=now,
        contributions=[],
        full_tex=True,
    )
    definition = rf"\definecolor{{shivablue}}{{HTML}}{{{expected}}}"
    assert definition in output
    assert output.index(definition) < output.index(r"\setbeamercolor{title}")


@pytest.mark.parametrize("color", ["", "red", "#123", "1234567", "##123456", "GG0000"])
def test_invalid_accent_color(color):
    with pytest.raises(pydantic.ValidationError, match="six hexadecimal digits"):
        Spec(accent_color=color)


def test_full_tex_uses_configured_title():
    since = datetime(2022, 8, 1, tzinfo=tzlocal())
    now = datetime(2022, 8, 11, tzinfo=tzlocal())
    output = generate_latex(
        Spec(title="Weekly sync", footline_left="Core team", repos=[]),
        {},
        since=since,
        now=now,
        contributions=[],
        full_tex=True,
    )

    assert r"\title{ Weekly sync }" in output
    assert r"\author{ Core team }" in output


def test_placeholders_in_title_and_footline():
    since = datetime(2022, 8, 1, tzinfo=tzlocal())
    now = datetime(2022, 8, 11, tzinfo=tzlocal())
    repo = Repository(name="acts-project/acts")
    data = {
        "acts-project/acts": {
            "merged_prs": [],
            "open_prs": [],
            "stale": [],
            "recent_issues": [],
            "needs_discussion": [],
            "merged_prs_by_category": [],
            "open_prs_by_category": [],
            "release_tag": "v1.2.3",
            "release_stats": None,
            "spec": repo,
        }
    }

    output = generate_latex(
        Spec(
            title="ACTS {release}",
            footline_left="Core team -- {release}",
            repos=[repo],
        ),
        data,
        since=since,
        now=now,
        contributions=[],
        full_tex=True,
    )

    assert r"\title{ ACTS v1.2.3 }" in output
    assert r"\author{ Core team -- v1.2.3 }" in output


def test_placeholders_expand():
    since = datetime(2022, 8, 1, tzinfo=tzlocal())
    now = datetime(2022, 8, 11, tzinfo=tzlocal())
    values = build_placeholders(
        {
            "acts-project/acts": {
                "release_tag": None,
                "spec": Repository(name="acts-project/acts", display_name="ACTS"),
            }
        },
        since,
        now,
    )

    assert values == {
        "release": "",
        "repos": "ACTS",
        "since": "2022-08-01",
        "date": "2022-08-11",
        "range": "between 2022-08-01 and 2022-08-11",
    }

    # A placeholder that expands to nothing takes its separator with it.
    assert expand_placeholders("Core team -- {release}", values) == "Core team"
    assert expand_placeholders("{release}: weekly", values) == "weekly"
    assert expand_placeholders("Update for {repos}", values) == "Update for ACTS"
    # Unknown keywords are left alone, so LaTeX groups survive.
    assert (
        expand_placeholders(r"\textbf{bold} {nope}", values) == r"\textbf{bold} {nope}"
    )
    assert expand_placeholders(None, values) is None


# check if we have latexmk
have_latexmk = False
try:
    latexmk_path = Path(subprocess.check_output(["which", "latexmk"]).decode().strip())
    if latexmk_path.exists():
        have_latexmk = True
except:
    pass


@pytest.mark.skipif(not have_latexmk, reason="latexmk not found")
@pytest.mark.asyncio
@pytest.mark.parametrize("full_tex", [True, False], ids=["full", "fragment"])
async def test_compile(full_tex, tmp_path):
    since = datetime(2022, 8, 1, tzinfo=tzlocal())
    now = datetime(2022, 8, 11, tzinfo=tzlocal())

    with (Path(__file__).parent / "acts_spec.yml").open() as fh:
        spec = Spec.model_validate(yaml.safe_load(fh))

    ref = Path(__file__).parent / "ref"

    def get_file_content(file: str, cls):
        with (ref / file).open() as fh:
            return [cls.model_validate(o) for o in json.load(fh)]

    forge = FakeForge(
        merged_prs=get_file_content("merged_prs.json", PullRequest),
        open_prs=get_file_content("open_prs.json", Issue),
        recent_issues=get_file_content("recent_issues.json", Issue),
        labelled={"Stale": get_file_content("stale.json", Issue)},
    )

    result = await mtng.collect.collect_repositories(
        spec.repos, forge=forge, since=since, now=now
    )

    tex = generate_latex(
        Spec(repos=spec.repos),
        result,
        since=since,
        now=now,
        contributions=[],
        full_tex=full_tex,
    )
    source = tmp_path / "source.tex"

    with source.open("w") as fh:
        if not full_tex:
            fh.write("\\documentclass{beamer}\n\\begin{document}\n")
        fh.write(tex)
        if not full_tex:
            fh.write("\n\\end{document}")

    try:
        subprocess.check_call(
            [
                latexmk_path,
                f"-output-directory={tmp_path/'build'}",
                "-halt-on-error",
                "-pdf",
                str(source),
            ]
        )
    except subprocess.CalledProcessError:
        print(source)
        raise

    print(tmp_path / "build" / "source.pdf")


@pytest.fixture
def try_render(tmp_path):
    if have_latexmk:
        n = 0

        def render(source):
            nonlocal n
            n += 1
            build_dir = tmp_path / f"{n}"
            file = build_dir / "source.tex"
            build_dir.mkdir()
            file.write_text(source)
            try:
                subprocess.check_output(
                    [
                        latexmk_path,
                        f"-output-directory={build_dir}",
                        "-pdf",
                        "-halt-on-error",
                        file,
                    ],
                    stderr=subprocess.STDOUT,
                )
            except subprocess.CalledProcessError as e:
                print(e.output.decode())
                print(file)
                return False
            outfile = build_dir / "source.pdf"
            assert outfile.exists()
            print(outfile)
            return True

    else:

        def render(source):
            return True

    return render


def test_item_render(try_render):
    repo = Repository(name="acts-project/acts")

    tpl = env.get_template("item.tex")
    ctpl = env.get_template("item_context.tex")

    user_a = User(login="someone", html_url="https://example.com")
    user_b = User(login="another", html_url="https://example.com")

    item = PullRequest(
        title="feat: Enable Delegates to conveniently use stateful lambdas",
        user=user_a,
        labels=[Label(name="good")],
        number=1234,
        html_url="https://example.com",
        url="https://example.com",
        reviews=[
            Review(user=user_b, state="APPROVED", body="", submitted_at=datetime.now())
        ],
        assignee=user_b,
        updated_at=datetime.now(),
        created_at=datetime.now() - timedelta(days=2),
        closed_at=None,
        is_wip=False,
        is_stale=False,
        draft=False,
        pull_request=[],
    )
    spec = Repository(name="acts-project/acts")
    spec.do_reviewers = True

    output = tpl.render(item=item, spec=spec, mode="MERGED", extra="EXTRA")
    assert "\\prmerged" in output
    assert "\\prwip" not in output
    assert "\\prstale" not in output
    assert "\\prcategory" not in output
    assert "EXTRA" in output
    from mtng.generate import title_characters

    assert str(title_characters("@" + user_a.login)) in output
    assert str(title_characters("@" + user_b.login)) not in output
    assert "approved" in output
    assert try_render(ctpl.render(item=item, spec=spec, mode="MERGED", extra="EXTRA"))

    item.pr_category_label = "feat"
    item.pr_category_color = "LimeGreen"
    output = tpl.render(item=item, spec=spec, mode="MERGED", extra="EXTRA")
    assert "\\prcategory" in output
    assert "feat" in output

    item.pr_category_breaking = True
    output = tpl.render(item=item, spec=spec, mode="MERGED", extra="EXTRA")
    assert r"\prbreaking{alertred}" in output

    spec.show_review_summary = False
    output = tpl.render(item=item, spec=spec, mode="MERGED", extra="EXTRA")
    assert "approved" not in output
    assert "reviewed by" not in output
    assert "comment by" not in output
    assert "changes requested by" not in output

    item.is_wip = True
    output = tpl.render(item=item, spec=spec, mode="MERGED", extra="EXTRA")
    assert "\\prmerged" in output
    assert "\\prwip" in output
    assert "\\prstale" not in output
    assert "EXTRA" in output
    assert try_render(ctpl.render(item=item, spec=spec, mode="MERGED", extra="EXTRA"))

    item.is_stale = True
    output = tpl.render(item=item, spec=spec, mode="MERGED", extra="EXTRA")
    assert "\\prmerged" in output
    assert "\\prwip" in output
    assert "\\prstale" in output
    assert "EXTRA" in output
    assert try_render(ctpl.render(item=item, spec=spec, mode="MERGED", extra="EXTRA"))

    output = tpl.render(item=item, spec=spec, mode="OPEN", extra="EXTRA")
    assert "\\propen" in output
    assert "EXTRA" in output
    assert try_render(ctpl.render(item=item, spec=spec, mode="MERGED", extra="EXTRA"))

    spec.do_assignee = True

    item = Issue(
        title="Fatras: Bethe-Heitler calculation wrong?",
        user=user_a,
        labels=[],
        html_url="https://example.com",
        url="https://example.com",
        number=1234,
        assignee=user_b,
        updated_at=datetime.now(),
        created_at=datetime.now() - timedelta(days=2),
        closed_at=None,
    )

    output = tpl.render(item=item, spec=spec, mode=None, extra="EXTRA")
    assert "\\iss" in output
    from mtng.generate import title_characters

    assert str(title_characters("@" + user_a.login)) in output
    assert str(title_characters("@" + user_b.login)) in output
    assert "EXTRA" in output
    assert try_render(ctpl.render(item=item, spec=spec, mode="MERGED", extra="EXTRA"))

    item.assignee = None
    output = tpl.render(item=item, spec=spec, mode=None, extra="EXTRA")
    assert user_b.login not in output
    assert "no assignee" in output
    assert "EXTRA" in output
    assert try_render(ctpl.render(item=item, spec=spec, mode="MERGED", extra="EXTRA"))


prob = ["^", "_", "%", "#", "&", "<", ">", "$", "\\", "{", "}"]


@pytest.mark.parametrize(
    "prob", prob + [a + b for a, b in itertools.combinations_with_replacement(prob, 2)]
)
@pytest.mark.skipif(not have_latexmk, reason="latexmk not found")
def test_sanitization(try_render, prob):
    repo = Repository(name="acts-project/acts")

    ctpl = env.get_template("item_context.tex")

    user_a = User(login=f"someone_{prob}", html_url="https://example.com")
    user_b = User(login=f"another_{prob}", html_url="https://example.com")

    item = PullRequest(
        title=f"feat: I'm{prob} a {prob}PR: {prob} ",
        user=user_a,
        labels=[Label(name="good")],
        number=1234,
        html_url="https://example.com",
        url="https://example.com",
        assignee=user_b,
        updated_at=datetime.now(),
        created_at=datetime.now() - timedelta(days=2),
        closed_at=None,
        is_wip=False,
        is_stale=False,
        draft=False,
        pull_request=[],
    )
    spec = Repository(name="acts-project/acts")

    assert try_render(ctpl.render(item=item, spec=spec, mode="MERGED"))


def test_extract_release_pull_numbers():
    body = """
## What's Changed
* One in https://github.com/acts-project/acts/pull/123
* Two in https://github.com/acts-project/acts/pull/456
* Duplicate in https://github.com/acts-project/acts/pull/123
* Other repo in https://github.com/someone/other/pull/999
"""
    assert extract_release_pull_numbers(body, "acts-project/acts") == [123, 456]


def test_extract_release_pull_numbers_generated_notes_format():
    body = """
## 🚀 Features
- Re-establish detray navigation on ODD from Gen3 (#5579) by @asalzburger
- Add portal tagging blueprint node (#5593) by @paulgessinger
"""
    assert extract_release_pull_numbers(body, "acts-project/acts") == [5579, 5593]


def test_parse_pr_category():
    assert parse_pr_category("feat(parser): add grouped output") == ("feat", False)
    assert parse_pr_category("fix!: drop deprecated output") == ("fix", True)
    assert parse_pr_category("refactor:drop prefixless subject") == ("refactor", False)
    assert parse_pr_category("not conventional") == ("other", False)


def test_parse_pr_metadata():
    assert parse_pr_metadata("feat(ui): add badge") == (
        "feat",
        False,
        "add badge",
        True,
    )
    assert parse_pr_metadata("refactor!: cleanup") == (
        "refactor",
        True,
        "cleanup",
        True,
    )
    assert parse_pr_metadata("not conventional") == (
        "other",
        False,
        "not conventional",
        False,
    )


def test_grouped_pr_sections_render():
    since = datetime(2022, 8, 1, tzinfo=tzlocal())
    now = datetime(2022, 8, 11, tzinfo=tzlocal())
    repo_spec = Repository(name="acts-project/acts", group_prs_by_category=True)
    user = User(login="someone", html_url="https://example.com")
    pr = PullRequest(
        title="feat(core)!: break API",
        user=user,
        labels=[],
        number=1,
        html_url="https://example.com/pull/1",
        url="https://example.com/pull/1",
        updated_at=now,
        created_at=since,
        closed_at=now,
        pull_request=[],
        pr_category="feat",
        pr_category_label="Feature",
        pr_category_breaking=True,
        pr_category_color="alertred",
        pr_category_order=0,
    )
    data = {
        "acts-project/acts": {
            "merged_prs": [pr],
            "open_prs": [],
            "stale": [],
            "recent_issues": [],
            "needs_discussion": [],
            "release_tag": None,
            "release_stats": None,
            "merged_prs_by_category": [
                {
                    "category": "feat",
                    "title": "Feature",
                    "emoji": None,
                    "order": 0,
                    "items": [pr],
                }
            ],
            "open_prs_by_category": [],
            "spec": repo_spec,
        }
    }

    output = generate_latex(
        Spec(repos=[repo_spec]),
        data,
        since=since,
        now=now,
        contributions=[],
        full_tex=False,
    )
    # Section pages are titled with the category alone.
    assert "\\section{ Feature}" in output
    assert r"\prbreaking{alertred}" in output


def test_grouped_pr_section_shows_category_emoji():
    since = datetime(2022, 8, 1, tzinfo=tzlocal())
    now = datetime(2022, 8, 11, tzinfo=tzlocal())
    repo_spec = Repository(name="acts-project/acts", group_prs_by_category=True)
    user = User(login="someone", html_url="https://example.com")
    pr = PullRequest(
        title="fix: repair the thing",
        user=user,
        labels=[],
        number=1,
        html_url="https://example.com/pull/1",
        url="https://example.com/pull/1",
        updated_at=now,
        created_at=since,
        closed_at=now,
        pull_request=[],
    )
    mtng.collect.enrich_item(pr, repo_spec)
    (group,) = mtng.collect.group_prs_by_category([pr])

    assert group["title"] == "Bugfix"
    assert group["emoji"] == "bug"

    data = {
        "acts-project/acts": {
            "merged_prs": [pr],
            "open_prs": [],
            "stale": [],
            "recent_issues": [],
            "needs_discussion": [],
            "release_tag": None,
            "release_stats": None,
            "merged_prs_by_category": [group],
            "open_prs_by_category": [],
            "spec": repo_spec,
        }
    }

    output = generate_latex(
        Spec(repos=[repo_spec]),
        data,
        since=since,
        now=now,
        contributions=[],
        full_tex=False,
    )
    assert "\\section{ \\protect\\cusemoji{bug}{} Bugfix}" in output


def _merged_pr(title: str, repo_spec: Repository, number: int = 1) -> PullRequest:
    since = datetime(2022, 8, 1, tzinfo=tzlocal())
    now = datetime(2022, 8, 11, tzinfo=tzlocal())
    pr = PullRequest(
        title=title,
        user=User(login="someone", html_url="https://example.com"),
        labels=[],
        number=number,
        html_url="https://example.com/pull/1",
        url="https://example.com/pull/1",
        updated_at=now,
        created_at=since,
        closed_at=now,
    )
    mtng.collect.enrich_item(pr, repo_spec)
    return pr


def test_breaking_changes_section():
    repo_spec = Repository(name="acts-project/acts", group_prs_by_category=True)
    prs = [
        _merged_pr("feat!: break the API", repo_spec, number=1),
        _merged_pr("feat: add a thing", repo_spec, number=2),
    ]

    output = _render(repo_spec, _repo_data(repo_spec, prs))

    assert "\\section{ \\protect\\cusemoji{warning}{} Breaking changes}" in output
    assert "{\\reponame{}: Breaking changes}" in output
    # Repeated by default, so the breaking PR is on its category slide as well.
    assert output.count("break the API") == 2


def test_breaking_changes_not_repeated():
    repo_spec = Repository(
        name="acts-project/acts",
        group_prs_by_category=True,
        repeat_breaking_changes=False,
    )
    prs = [
        _merged_pr("feat!: break the API", repo_spec, number=1),
        _merged_pr("feat: add a thing", repo_spec, number=2),
    ]
    data = _repo_data(repo_spec, prs)

    output = _render(repo_spec, data)

    assert output.count("break the API") == 1
    assert "add a thing" in output


def test_breaking_changes_can_be_disabled():
    repo_spec = Repository(
        name="acts-project/acts",
        group_prs_by_category=True,
        show_breaking_changes=False,
    )
    prs = [_merged_pr("feat!: break the API", repo_spec, number=1)]

    output = _render(repo_spec, _repo_data(repo_spec, prs))

    assert "Breaking changes" not in output
    assert output.count("break the API") == 1


def test_breaking_changes_section_skipped_when_empty():
    repo_spec = Repository(name="acts-project/acts")
    prs = [_merged_pr("feat: add a thing", repo_spec, number=1)]

    output = _render(repo_spec, _repo_data(repo_spec, prs))

    assert "Breaking changes" not in output


@pytest.mark.asyncio
async def test_collect_splits_breaking_prs():
    repo = Repository(
        name="acts-project/acts",
        do_open_prs=False,
        repeat_breaking_changes=False,
    )
    breaking = _merged_pr("feat!: break the API", repo, number=1)
    normal = _merged_pr("feat: add a thing", repo, number=2)

    result = await mtng.collect.collect_repositories(
        [repo],
        since=datetime(2022, 8, 1, tzinfo=tzlocal()),
        now=datetime(2022, 8, 11, tzinfo=tzlocal()),
        forge=FakeForge(merged_prs=[breaking, normal]),
    )

    data = result["acts-project/acts"]
    assert data["breaking_prs"] == [breaking]
    assert data["merged_prs_listed"] == [normal]
    assert data["merged_prs"] == [breaking, normal]


def test_default_category_order():
    repo_spec = Repository(name="acts-project/acts")
    prs = [
        _merged_pr(f"{kind}: something", repo_spec, number=i)
        for i, kind in enumerate(["chore", "refactor", "fix", "perf", "feat"])
    ]

    groups = mtng.collect.group_prs_by_category(prs)

    assert [group["category"] for group in groups] == [
        "feat",
        "fix",
        "refactor",
        "perf",
        "chore",
    ]


def test_configured_category_order():
    repo_spec = Repository(
        name="acts-project/acts",
        group_prs_by_category=True,
        pr_category_order=["chore", "fix"],
    )
    prs = [
        _merged_pr(f"{kind}: something", repo_spec, number=i)
        for i, kind in enumerate(["feat", "fix", "chore", "docs"])
    ]

    groups = mtng.collect.group_prs_by_category(prs)

    # Listed categories first, everything else in the default order behind them.
    assert [group["category"] for group in groups] == ["chore", "fix", "feat", "docs"]


def _repo_data(repo_spec: Repository, prs, release_tag=None):
    breaking = [pr for pr in prs if pr.pr_category_breaking]
    listed = (
        prs
        if repo_spec.repeat_breaking_changes or not repo_spec.show_breaking_changes
        else [pr for pr in prs if not pr.pr_category_breaking]
    )
    return {
        repo_spec.name: {
            "merged_prs": prs,
            "merged_prs_listed": listed,
            "breaking_prs": breaking,
            "open_prs": [],
            "stale": [],
            "recent_issues": [],
            "needs_discussion": [],
            "release_tag": release_tag,
            "release_stats": None,
            "merged_prs_by_category": mtng.collect.group_prs_by_category(listed),
            "open_prs_by_category": [],
            "spec": repo_spec,
        }
    }


def _render(repo_spec, data):
    return generate_latex(
        Spec(repos=[repo_spec]),
        data,
        since=datetime(2022, 8, 1, tzinfo=tzlocal()),
        now=datetime(2022, 8, 11, tzinfo=tzlocal()),
        contributions=[],
        full_tex=False,
    )


def test_default_frame_titles():
    repo_spec = Repository(name="acts-project/acts")
    prs = [_merged_pr("feat: something", repo_spec)]

    output = _render(repo_spec, _repo_data(repo_spec, prs))

    # {category} is empty without grouping, and takes its parentheses with it.
    assert (
        "{\\reponame{}: PRs merged between 2022-08-01 and 2022-08-11}" in output
    ), output


def test_configured_frame_title():
    repo_spec = Repository(
        name="acts-project/acts",
        group_prs_by_category=True,
        frame_titles={"merged_prs": "What landed {range} -- {category}"},
    )
    prs = [_merged_pr("feat: something", repo_spec)]

    output = _render(repo_spec, _repo_data(repo_spec, prs))

    assert "{What landed between 2022-08-01 and 2022-08-11 -- Feature}" in output


def test_frame_title_uses_release_tag():
    repo_spec = Repository(
        name="acts-project/acts",
        frame_titles={"release_prs": "{repo} {release}"},
    )
    prs = [_merged_pr("feat: something", repo_spec)]

    output = _render(repo_spec, _repo_data(repo_spec, prs, release_tag="v1.2.3"))

    assert "{\\reponame{} v1.2.3}" in output


def test_frame_titles_single_string_applies_to_every_frame():
    repo_spec = Repository(
        name="acts-project/acts",
        group_prs_by_category=True,
        frame_titles="{repo} {category}",
    )

    assert repo_spec.frame_titles["merged_prs"] == "{repo} {category}"
    assert repo_spec.frame_titles["open_prs"] == "{repo} {category}"

    prs = [_merged_pr("feat: something", repo_spec)]
    output = _render(repo_spec, _repo_data(repo_spec, prs))

    assert "{\\reponame{} Feature}" in output


def test_frame_title_sanitizes_and_rejects_unknown_keys():
    repo_spec = Repository(
        name="acts-project/acts",
        frame_titles={"merged_prs": "100% merged & counting: {range}"},
    )
    prs = [_merged_pr("feat: something", repo_spec)]
    output = _render(repo_spec, _repo_data(repo_spec, prs))
    assert "100\\% merged \\& counting: between 2022-08-01 and 2022-08-11" in output

    with pytest.raises(pydantic.ValidationError, match="Unknown frame_titles key"):
        Repository(name="acts-project/acts", frame_titles={"merged": "nope"})


def test_resolve_category_label():
    assert resolve_category_label("feat", {"feat": "Feature"}) == "Feature"
    assert resolve_category_label("unknown", {"other": "Other"}) == "Other"


def test_item_render_uses_stripped_title_when_present():
    repo = Repository(name="acts-project/acts")
    tpl = env.get_template("item.tex")
    item = PullRequest(
        title="feat: Keep original in data",
        pr_title_display="Keep original in data",
        user=User(login="someone", html_url="https://example.com"),
        labels=[],
        number=1,
        html_url="https://example.com/pull/1",
        url="https://example.com/pull/1",
        updated_at=datetime.now(),
        created_at=datetime.now(),
        closed_at=datetime.now(),
        pull_request=[],
    )
    output = tpl.render(item=item, spec=repo, mode="MERGED", extra="")
    assert "Keep original in data" in output
    assert "feat: Keep original in data" not in output


def test_parse_release_tag():
    assert parse_release_tag("v47.0.0", "acts-project/acts") == "v47.0.0"
    assert (
        parse_release_tag(
            "https://github.com/acts-project/acts/releases/tag/v47.0.0",
            "acts-project/acts",
        )
        == "v47.0.0"
    )

    with pytest.raises(ValueError):
        parse_release_tag(
            "https://github.com/other/repo/releases/tag/v47.0.0",
            "acts-project/acts",
        )


def test_review_model_accepts_pending_state():
    review = Review.model_validate(
        {
            "user": {"login": "octocat", "html_url": "https://github.com/octocat"},
            "state": "PENDING",
            "body": None,
            "submitted_at": None,
        }
    )
    assert review.state == "PENDING"


@pytest.mark.asyncio
async def test_collect_repositories_release():
    repo = Repository(
        name="acts-project/acts",
        do_open_prs=False,
        do_recent_issues=False,
        stale_label=None,
        do_merged_prs=True,
    )

    ref = Path(__file__).parent / "ref"
    with (ref / "merged_prs.json").open() as fh:
        merged = [PullRequest.model_validate(json.load(fh)[0])]

    forge = FakeForge(release=("v47.0.0", merged))

    since = datetime(2022, 8, 1, tzinfo=tzlocal())
    now = datetime(2022, 8, 11, tzinfo=tzlocal())
    result = await collect_repositories(
        [repo], since=since, now=now, forge=forge, release="v47.0.0"
    )

    repo_data = result["acts-project/acts"]
    assert repo_data["release_tag"] == "v47.0.0"
    assert len(repo_data["merged_prs"]) == 1
    assert len(repo_data["merged_prs_by_category"]) == 1
    assert repo_data["merged_prs"][0].pr_category is not None


@pytest.mark.asyncio
async def test_release_mode_only_outputs_release_prs():
    repo = Repository(
        name="acts-project/acts",
        do_open_prs=True,
        do_recent_issues=True,
        stale_label="Stale",
        needs_discussion_label="Needs Discussion",
        do_merged_prs=True,
    )

    ref = Path(__file__).parent / "ref"
    with (ref / "merged_prs.json").open() as fh:
        merged = [PullRequest.model_validate(json.load(fh)[0])]

    forge = FakeForge(release=("v47.0.0", merged))

    since = datetime(2022, 8, 1, tzinfo=tzlocal())
    now = datetime(2022, 8, 11, tzinfo=tzlocal())
    result = await collect_repositories(
        [repo], since=since, now=now, forge=forge, release="v47.0.0"
    )
    output = generate_latex(
        Spec(repos=[repo]),
        result,
        since=since,
        now=now,
        contributions=[],
        full_tex=False,
    )

    assert forge.calls == ["release_prs"]
    assert "Release v47.0.0" in output
    assert "Open PRs" not in output
    assert "Issues opened since" not in output
    assert "Stale Issues and PRs" not in output

    # The bento statistics frame is rendered from release_stats.
    assert "\\bentobox" in output
    assert "merged PRs" in output
    assert "contributors" in output
    # merged_prs.json predates the churn fields, so the whole churn row must be
    # dropped rather than rendering tiles full of dashes.
    assert "lines added" not in output
    assert "lines removed" not in output


@pytest.mark.asyncio
async def test_release_bento_renders_churn():
    repo = Repository(name="acts-project/acts", do_merged_prs=True)

    user_a = User(login="alice", html_url="https://example.com")
    user_b = User(login="bob", html_url="https://example.com")

    def make(number, author, additions, deletions, files, commits, day):
        return PullRequest(
            title=f"PR {number}",
            user=author,
            labels=[Label(name="Feature")],
            number=number,
            html_url="https://example.com",
            url="https://example.com",
            reviews=[Review(user=user_b, state="APPROVED")],
            updated_at=datetime(2022, 8, day, tzinfo=tzlocal()),
            created_at=datetime(2022, 8, 1, tzinfo=tzlocal()),
            closed_at=datetime(2022, 8, day, tzinfo=tzlocal()),
            merged_at=datetime(2022, 8, day, tzinfo=tzlocal()),
            additions=additions,
            deletions=deletions,
            changed_files=files,
            commits=commits,
        )

    merged = [
        make(1, user_a, 1200, 300, 10, 5, 2),
        make(2, user_b, 34, 12, 2, 1, 6),
    ]

    forge = FakeForge(release=("v47.0.0", merged))

    since = datetime(2022, 8, 1, tzinfo=tzlocal())
    now = datetime(2022, 8, 11, tzinfo=tzlocal())
    result = await collect_repositories(
        [repo], since=since, now=now, forge=forge, release="v47.0.0"
    )
    output = generate_latex(
        Spec(repos=[repo]),
        result,
        since=since,
        now=now,
        contributions=[],
        full_tex=False,
    )

    assert "\\bentobox" in output
    assert "lines added" in output
    assert "lines removed" in output
    assert "1\\,234" in output  # 1200 + 34 additions, thin-space separated
    assert "312" in output  # deletions
    assert "files changed" in output
    assert "commits" in output
    # Two authors, and bob reviewed but is also an author, so he is not counted.
    assert "contributors" in output
    assert "reviewers" in output
    assert "top labels" in output
    assert "Feature" in output
    # 2 Aug -> 6 Aug inclusive
    assert "days, Aug 02--Aug 06" in output


def test_bento_present_outside_release_mode():
    """Regular period-based reports should also render the bento frame."""
    reference = (Path(__file__).parent / "ref" / "reference.tex").read_text()
    assert "\\bentobox{" in reference
    assert "period" in reference
    assert "reporting period" in reference


@pytest.mark.skipif(not have_latexmk, reason="latexmk not found")
@pytest.mark.parametrize("full_tex", [True, False], ids=["full", "fragment"])
@pytest.mark.asyncio
@pytest.mark.parametrize("truncate", [False, True])
async def test_compile_release_bento(full_tex, tmp_path, truncate):
    """Compile the bento frame for real.

    This is the only thing that catches pgfmath / \\dimexpr mistakes in
    \\bentogeom. The fragment case additionally exercises the provides.tex
    fallbacks, which must compile in a deck that never loaded tikz.
    """
    repo = Repository(
        name="acts-project/acts", do_merged_prs=True, truncate_pr_titles=truncate
    )

    user = User(login="alice", html_url="https://example.com")
    merged = [
        PullRequest(
            title="feat: " + "a long title with $ and _ and {braces} " * 8,
            user=user,
            labels=[Label(name="Feature"), Label(name="bug_report")],
            number=1234,
            html_url="https://example.com",
            url="https://example.com",
            reviews=[
                Review(
                    user=User(login="bob", html_url="https://example.com"),
                    state="APPROVED",
                )
            ],
            updated_at=datetime(2022, 8, 2, tzinfo=tzlocal()),
            created_at=datetime(2022, 8, 1, tzinfo=tzlocal()),
            closed_at=datetime(2022, 8, 2, tzinfo=tzlocal()),
            merged_at=datetime(2022, 8, 2, tzinfo=tzlocal()),
            additions=12345,
            deletions=678,
            changed_files=42,
            commits=9,
        )
    ]

    # A tag with LaTeX-hostile characters, to pin the sanitize fix.
    forge = FakeForge(release=("v47.0.0_rc1", merged))

    since = datetime(2022, 8, 1, tzinfo=tzlocal())
    now = datetime(2022, 8, 11, tzinfo=tzlocal())
    result = await collect_repositories(
        [repo], since=since, now=now, forge=forge, release="v47.0.0_rc1"
    )
    tex = generate_latex(
        Spec(repos=[repo]),
        result,
        since=since,
        now=now,
        contributions=[],
        full_tex=full_tex,
    )

    source = tmp_path / "source.tex"
    with source.open("w") as fh:
        if not full_tex:
            fh.write("\\documentclass{beamer}\n\\begin{document}\n")
        fh.write(tex)
        if not full_tex:
            fh.write("\n\\end{document}")

    try:
        subprocess.check_output(
            [
                latexmk_path,
                f"-output-directory={tmp_path/'build'}",
                "-halt-on-error",
                "-pdf",
                str(source),
            ],
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as e:
        print(e.output.decode())
        print(source)
        raise

    assert (tmp_path / "build" / "source.pdf").exists()


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "--"),
        (0, "0"),
        (999, "999"),
        (1234, "1\\,234"),
        (12345, "12.3k"),
        (1234567, "1235k"),
    ],
)
def test_human_int(value, expected):
    from mtng.generate import human_int

    assert str(human_int(value)) == expected


def test_title_characters_preserves_escape_units():
    from mtng.generate import title_characters

    assert str(title_characters("a_$ {}")) == r"{a}{\_}{\$}{ }{\{}{\}}"


def test_pr_title_truncation_is_optional():
    repo = Repository(name="acts-project/acts", truncate_pr_titles=True)
    output = _render(repo, _repo_data(repo, [_merged_pr("feat: Short title", repo)]))
    assert "\\mtngprline{" in output
    assert "{S}{h}{o}{r}{t}{ }{t}{i}{t}{l}{e}" in output
    repo.truncate_pr_titles = False
    output = _render(repo, _repo_data(repo, [_merged_pr("feat: Short title", repo)]))
    assert "\\mtngprline{" not in output


@pytest.mark.parametrize("truncate", [False, True])
@pytest.mark.parametrize("additions,deletions", [(1234, 56), (0, 0), (None, None)])
def test_pr_diff_size(truncate, additions, deletions):
    repo = Repository(name="acts-project/acts", truncate_pr_titles=truncate)
    pr = _merged_pr("feat: Diff size example", repo)
    pr.additions, pr.deletions = additions, deletions
    output = _render(repo, _repo_data(repo, [pr]))
    if additions is None:
        assert "green!50!black" not in output
    else:
        added = "1\\,234" if additions else "0"
        assert rf"\textcolor{{green!50!black}}{{+{added}}}" in output
        assert rf"\textcolor{{red!75!black}}{{\ensuremath{{-}}{deletions}}}" in output


@pytest.mark.parametrize(
    "states,expected",
    [
        ([], "awaiting review"),
        (["APPROVED"], "approved"),
        (["CHANGES_REQUESTED", "APPROVED"], "approved"),
        (["APPROVED", "CHANGES_REQUESTED"], "changes requested"),
        (["APPROVED", "COMMENTED"], "approved"),
        (["APPROVED", "DISMISSED"], "awaiting review"),
        (["PENDING"], "awaiting review"),
    ],
)
def test_compact_review_latest_decisive_state(states, expected):
    from mtng.generate import review_status

    repo = Repository(name="acts-project/acts")
    pr = _merged_pr("feat: review status", repo)
    user = User(login="reviewer", html_url="https://example.com")
    start = datetime(2026, 10, 1)
    pr.reviews = [
        Review(user=user, state=state, submitted_at=start + timedelta(days=i))
        for i, state in enumerate(states)
    ][
        ::-1
    ]  # Must not depend on API ordering.
    assert review_status(pr)[0] == expected
    pr.reviews_available = False
    assert review_status(pr)[0] == "review unavailable"


def test_compact_review_change_request_takes_precedence():
    from mtng.generate import review_status

    pr = _merged_pr("feat: review status", Repository(name="acts-project/acts"))
    pr.reviews = [
        Review(user=User(login=name, html_url="https://example.com"), state=state)
        for name, state in [("alice", "CHANGES_REQUESTED"), ("bob", "APPROVED")]
    ]
    assert review_status(pr)[0] == "changes requested"


def test_age_days_uses_report_timezone_and_clamps_future_dates():
    from datetime import timezone
    from mtng.generate import age_days

    now = datetime(2026, 10, 14, 1, tzinfo=timezone(timedelta(hours=2)))
    assert age_days(datetime(2026, 10, 13, 23, tzinfo=timezone.utc), now) == 0
    assert age_days(now - timedelta(days=18), now) == 18
    assert age_days(now + timedelta(days=1), now) == 0


def test_word_truncation_units_keep_words_and_escapes_intact():
    from mtng.generate import title_units, title_characters

    assert str(title_units("preserve input order")) == "{preserve }{input }{order}"
    assert str(title_units("handle $ and _")) == r"{handle }{\$ }{and }{\_}"
    assert title_units("LongSingleIdentifier") == title_characters(
        "LongSingleIdentifier"
    )


def test_draft_icon_and_file_count():
    repo = Repository(name="acts-project/acts", do_reviewers=True)
    pr = _merged_pr("feat: draft", repo)
    pr.draft = True
    pr.is_wip = True
    pr.changed_files = 1
    output = env.get_template("item.tex").render(
        item=pr, spec=repo, mode="OPEN", extra=""
    )
    assert r"\prdraft" in output
    assert r"\prcategory{Draft}{black!45}" in output
    assert r"\prwip" not in output
    assert "1 file" in output
    assert "1 files" not in output
    assert "awaiting review" in output
    pr.changed_files = None
    output = env.get_template("item.tex").render(
        item=pr, spec=repo, mode="OPEN", extra=""
    )
    assert "1 file" not in output


@pytest.mark.parametrize(
    "day,new", [(0, False), (1, True), (5, True), (11, True), (12, False)]
)
def test_new_pr_badge_uses_inclusive_report_window(day, new):
    repo = Repository(name="acts-project/acts")
    pr = _merged_pr("feat: new PR", repo)
    pr.created_at = datetime(2022, 8, 1, tzinfo=tzlocal()) + timedelta(days=day - 1)
    data = _repo_data(repo, [])
    data[repo.name]["open_prs"] = [pr]
    output = _render(repo, data)
    assert (r"\prnew\prcategory" in output) is new


@pytest.mark.skipif(not have_latexmk, reason="latexmk not found")
@pytest.mark.asyncio
async def test_busy_pr_rows_wrap_without_overflow(tmp_path):
    repo = Repository(
        name="example/project",
        do_merged_prs=False,
        do_reviewers=True,
        do_assignee=True,
        show_wip=True,
        truncate_pr_titles=True,
    )
    now = datetime(2026, 10, 14, tzinfo=tzlocal())
    user = User(login="long-account-name-" * 4, html_url="https://example.com")
    prs = [
        PullRequest(
            title="feat: " + "long title with words and escaped $ _ characters " * 5,
            user=user,
            assignee=user,
            labels=[],
            number=i,
            url="https://example.com",
            html_url="https://example.com",
            created_at=now - timedelta(days=2),
            updated_at=now,
            draft=i % 2 == 0,
            additions=98765,
            deletions=54321,
            changed_files=1234,
            reviews=[Review(user=user, state="CHANGES_REQUESTED")],
        )
        for i in range(16)
    ]
    since = now - timedelta(days=7)
    data = await collect_repositories([repo], since, now, FakeForge(open_prs=prs))
    tex = generate_latex(Spec(repos=[repo]), data, since, now, [], True)
    # Test PR pagination; exclude the independent title-page layout.
    tex = tex.replace(r"\frame[plain]{\titlepage}", "")
    source = tmp_path / "overflow.tex"
    source.write_text(tex)
    subprocess.check_output(
        [
            str(latexmk_path),
            "-pdf",
            "-halt-on-error",
            f"-outdir={tmp_path}",
            str(source),
        ],
        stderr=subprocess.STDOUT,
    )
    log = source.with_suffix(".log").read_text()
    assert "Overfull" not in log
    assert source.with_suffix(".pdf").exists()


@pytest.mark.parametrize(
    "days,expected", [(1, "a day"), (2, "2 days"), (18, "18 days"), (365, "a year")]
)
def test_human_readable_days(days, expected):
    from mtng.generate import human_days

    assert human_days(days) == expected


@pytest.mark.parametrize(
    "age,inactive,expected",
    [
        (0, 0, "opened today"),
        (18, 0, "open for 18 days"),
        (18, 1, "open for 18 days, inactive for a day"),
        (365, 7, "open for a year, inactive for 7 days"),
    ],
)
def test_open_pr_human_readable_age(age, inactive, expected):
    repo = Repository(name="acts-project/acts")
    pr = _merged_pr("feat: age display", repo)
    now = datetime(2022, 8, 11, tzinfo=tzlocal())
    pr.created_at = now - timedelta(days=age)
    pr.updated_at = now - timedelta(days=inactive)
    data = _repo_data(repo, [])
    data[repo.name]["open_prs"] = [pr]
    output = _render(repo, data)
    assert expected in output
    if inactive == 0:
        assert "inactive" not in output


@pytest.mark.parametrize("mode", ["OPEN", "MERGED"])
@pytest.mark.parametrize(
    "state,available,label,color",
    [
        ("APPROVED", True, "approved", "green!50!black"),
        ("CHANGES_REQUESTED", True, "changes requested", "red!75!black"),
        ("COMMENTED", True, "awaiting review", "black!55"),
        ("APPROVED", False, "review unavailable", "black!55"),
    ],
)
def test_review_status_presentation_is_uniform(mode, state, available, label, color):
    repo = Repository(name="acts-project/acts", do_reviewers=True)
    pr = _merged_pr("feat: uniform review status", repo)
    pr.reviews = [
        Review(
            user=User(
                login="hidden-reviewer", html_url="https://example.com/hidden-reviewer"
            ),
            state=state,
        )
    ]
    pr.reviews_available = available
    output = env.get_template("item.tex").render(
        item=pr, spec=repo, mode=mode, extra=""
    )
    assert rf"\textcolor{{{color}}}{{{label}}}" in output
    assert r"\mbox{\scriptsize\textcolor{black!55}{\ensuremath{\cdot}}" in output
    assert "hidden-reviewer" not in output
    assert "reviewed by" not in output


@pytest.mark.parametrize("kind,color", [("feat", "LimeGreen"), ("fix", "Purple")])
def test_breaking_marker_keeps_category_color(kind, color):
    repo = Repository(name="acts-project/acts")
    pr = _merged_pr(f"{kind}!: breaking title", repo)
    assert pr.pr_category_color == color
    rendered = env.from_string(
        '{% from "macros.tex" import show_item %}'
        '{% call show_item(item, spec, mode="OPEN", is_new=true) %}{% endcall %}'
    ).render(item=pr, spec=repo)
    assert r"\prnew\prbreaking{alertred}\prcategory{" in rendered
    assert f"}}{{{color}}}" in rendered
    assert r"\prcategory{\cusemoji" not in rendered
