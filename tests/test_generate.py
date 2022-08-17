from re import sub
from unittest.mock import Mock
import asyncio
from datetime import datetime
import json
from pathlib import Path
import os
import warnings
import subprocess

import pytest
import aiohttp
from gidgethub.aiohttp import GitHubAPI
import yaml

import mtng.collect
from mtng.generate import generate_latex
from mtng.spec import Repository, Spec


@pytest.mark.asyncio
async def test_generate(monkeypatch: pytest.MonkeyPatch, tmp_path):
    gh = Mock()

    repo = Repository(
        name="acts-project/acts",
        do_stale=True,
        stale_label="Stale",
        wip_label=":construction: WIP",
    )

    ref = Path(__file__).parent / "ref"

    def get_file_content(file: str):
        f = asyncio.Future()
        with (ref / file).open() as fh:
            f.set_result(json.load(fh))
        return f

    monkeypatch.setattr(
        "mtng.collect.get_merged_pulls",
        Mock(return_value=get_file_content("merged_prs.json")),
    )
    monkeypatch.setattr(
        "mtng.collect.get_open_issues",
        Mock(
            side_effect=[
                get_file_content("open_prs.json"),
                get_file_content("stale.json"),
                get_file_content("recent_issues.json"),
            ]
        ),
    )
    since = datetime(2022, 8, 1)
    now = datetime(2022, 8, 11)
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
    #  print((ref / "reference.tex").read_text())


@pytest.mark.asyncio
async def test_collect():
    repo = Repository(
        name="acts-project/acts",
        do_stale=True,
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
            [repo], gh=gh, since=datetime(2022, 8, 1), now=datetime(2022, 8, 2)
        )


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

    since = datetime(2022, 8, 1)
    now = datetime(2022, 8, 11)

    with (Path(__file__).parent / "acts_spec.yml").open() as fh:
        spec = Spec.parse_obj(yaml.safe_load(fh))

    if "GH_TOKEN" not in os.environ:
        warnings.warn(
            "GH_TOKEN environment variable not found. API based tests will likely fail"
        )

    async with aiohttp.ClientSession(loop=asyncio.get_event_loop()) as session:
        gh = GitHubAPI(session, __name__, oauth_token=os.environ["GH_TOKEN"])
        result = await mtng.collect.collect_repositories(
            spec.repos, gh=gh, since=datetime(2022, 8, 1), now=datetime(2022, 8, 2)
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
