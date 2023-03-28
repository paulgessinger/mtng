from asyncio.subprocess import STDOUT
import itertools
from re import sub
import shutil
from unittest.mock import Mock
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
import os
import warnings
import subprocess

import pytest
import aiohttp
from gidgethub.aiohttp import GitHubAPI
import yaml
from dateutil.tz import tzlocal

import mtng.collect
from mtng.generate import generate_latex, env
from mtng.spec import Repository, Spec
from mtng.collect import Label, PullRequest, Issue, User, get_open_pulls


@pytest.mark.asyncio
async def test_generate(monkeypatch: pytest.MonkeyPatch, tmp_path):
    gh = Mock()

    repo = Repository(
        name="acts-project/acts",
        stale_label="Stale",
        wip_label=":construction: WIP",
    )

    ref = Path(__file__).parent / "ref"

    def get_file_content(file: str, cls):
        f = asyncio.Future()
        with (ref / file).open() as fh:
            f.set_result([cls.parse_obj(o) for o in json.load(fh)])
        return f

    monkeypatch.setattr(
        "mtng.collect.get_merged_pulls",
        Mock(return_value=get_file_content("merged_prs.json", PullRequest)),
    )
    monkeypatch.setattr(
        "mtng.collect.get_open_issues",
        Mock(
            side_effect=[
                get_file_content("open_prs.json", Issue),
                get_file_content("stale.json", Issue),
                get_file_content("recent_issues.json", Issue),
            ]
        ),
    )
    since = datetime(2022, 8, 1, tzinfo=tzlocal())
    now = datetime(2022, 8, 11, tzinfo=tzlocal())
    result = await mtng.collect.collect_repositories(
        [repo], since=since, now=now, gh=gh
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
    assert output == ref_file.read_text(), str(act_file)


@pytest.mark.asyncio
async def test_collect(tmp_path):
    repo = Repository(
        name="acts-project/acts",
        stale_label="Stale",
        wip_label=":construction: WIP",
    )

    if "GH_TOKEN" not in os.environ:
        warnings.warn(
            "GH_TOKEN environment variable not found. API based tests will likely fail"
        )

    async with aiohttp.ClientSession(loop=asyncio.get_event_loop()) as session:
        gh = GitHubAPI(session, __name__, oauth_token=os.environ["GH_TOKEN"])
        result = await mtng.collect.collect_repositories(
            [repo],
            gh=gh,
            since=datetime(2022, 8, 1, tzinfo=tzlocal()),
            now=datetime(2022, 8, 11, tzinfo=tzlocal()),
        )

    (repo,) = result.values()
    for k in ["merged_prs", "open_prs", "stale", "recent_issues"]:
        outf = tmp_path / f"{k}.json"
        print(outf)
        with outf.open("w") as fh:
            json.dump([json.loads(o.json()) for o in repo[k]], fh, indent=2)


@pytest.mark.asyncio
async def test_get_open_pulls():
    repo = Repository(
        name="acts-project/acts",
        stale_label="Stale",
        wip_label=":construction: WIP",
    )

    async with aiohttp.ClientSession(loop=asyncio.get_event_loop()) as session:
        gh = GitHubAPI(session, __name__, oauth_token=os.environ["GH_TOKEN"])
        open_prs = await get_open_pulls(
            gh,
            repo.name,
            without_labels=repo.filter_labels,
        )

        for pr in open_prs:
            print(pr.json(indent=2))


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
async def test_compile(monkeypatch, full_tex, tmp_path):

    since = datetime(2022, 8, 1, tzinfo=tzlocal())
    now = datetime(2022, 8, 11, tzinfo=tzlocal())

    with (Path(__file__).parent / "acts_spec.yml").open() as fh:
        spec = Spec.parse_obj(yaml.safe_load(fh))

    ref = Path(__file__).parent / "ref"

    def get_file_content(file: str, cls):
        f = asyncio.Future()
        with (ref / file).open() as fh:
            f.set_result([cls.parse_obj(o) for o in json.load(fh)])
        return f

    monkeypatch.setattr(
        "mtng.collect.get_merged_pulls",
        Mock(return_value=get_file_content("merged_prs.json", PullRequest)),
    )
    monkeypatch.setattr(
        "mtng.collect.get_open_issues",
        Mock(
            side_effect=[
                get_file_content("open_prs.json", Issue),
                get_file_content("stale.json", Issue),
                get_file_content("recent_issues.json", Issue),
            ]
        ),
    )

    gh = Mock()
    result = await mtng.collect.collect_repositories(
        spec.repos, gh=gh, since=since, now=now
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
        assignee=user_b,
        updated_at=datetime.now(),
        created_at=datetime.now() - timedelta(days=2),
        closed_at=None,
        is_wip=False,
        is_stale=False,
        pull_request=[],
    )
    spec = Repository(name="acts-project/acts")

    output = tpl.render(item=item, spec=spec, mode="MERGED", extra="EXTRA")
    assert "\\prmerged" in output
    assert "\\prwip" not in output
    assert "\\prstale" not in output
    assert "EXTRA" in output
    assert user_a.login in output
    assert user_b.login in output
    assert try_render(ctpl.render(item=item, spec=spec, mode="MERGED", extra="EXTRA"))

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

    item = Issue(
        title="Fatras: Bethe-Heitler calculation wrong?",
        user=user_a,
        labels=[],
        html_url="https://example.com",
        number=1234,
        assignee=user_b,
        updated_at=datetime.now(),
        created_at=datetime.now() - timedelta(days=2),
        closed_at=None,
    )

    output = tpl.render(item=item, spec=spec, mode=None, extra="EXTRA")
    assert "\\iss" in output
    assert user_a.login in output
    assert user_b.login in output
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
        assignee=user_b,
        updated_at=datetime.now(),
        created_at=datetime.now() - timedelta(days=2),
        closed_at=None,
        is_wip=False,
        is_stale=False,
        pull_request=[],
    )
    spec = Repository(name="acts-project/acts")

    assert try_render(ctpl.render(item=item, spec=spec, mode="MERGED"))
