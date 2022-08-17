from unittest.mock import Mock
import asyncio
from datetime import datetime
import json
from pathlib import Path
import os
import warnings

import pytest
import aiohttp
from gidgethub.aiohttp import GitHubAPI

import mtng.collect
from mtng.generate import generate_latex
from mtng.spec import Repository, Spec


@pytest.mark.asyncio
async def test_generate(monkeypatch: pytest.MonkeyPatch):
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
    result = await mtng.collect.collect_repositories(
        [repo], since=since, now=datetime(2022, 8, 11), gh=gh
    )

    output = generate_latex(
        Spec(repos=[repo]), result, last=since, contributions=[], full_tex=False
    )

    output += "\n"  # newline at end of file

    assert output == (ref / "reference.tex").read_text()
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
            [repo], gh=gh, since=datetime(2022, 8, 1), now=datetime(2022, 8, 11)
        )
