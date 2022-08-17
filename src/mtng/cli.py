import os
from typing import Optional, List
import functools
import asyncio
import datetime
from pathlib import Path
import re
import urllib.parse
import json

import typer
from dotenv import load_dotenv
from gidgethub.aiohttp import GitHubAPI
import gidgethub
import aiohttp
import dateutil.parser
import yaml
import pydantic.schema

from mtng.generate import generate_latex
from mtng.spec import Spec
from mtng.collect import collect_repositories
from mtng.generate import env
from mtng import __version__

load_dotenv(dotenv_path=Path.cwd() / ".env")

cli = typer.Typer()


def make_sync(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        loop = asyncio.get_event_loop()
        loop.run_until_complete(fn(*args, **kwargs))

    return wrapped


async def collect(gh: GitHubAPI, repo: str, dt: datetime.datetime):
    merged_prs = [
        gh.getitem(f"/repos/{repo}/pulls/{issue['number']}")
        async for issue in gh.getiter(
            f"/search/issues?q=repo:{repo}+is:pr+merged:>={dt.strftime('%Y-%m-%d')}",
        )
    ]

    open_prs = [
        gh.getitem(f"/repos/{repo}/pulls/{issue['number']}")
        async for issue in gh.getiter(
            f'/search/issues?q=repo:{repo}+is:pr+is:open+-label:":construction: WIP"',
        )
    ]

    stale = [
        issue
        async for issue in gh.getiter(
            f"/search/issues?q=repo:{repo}+is:open+label:Stale"
        )
    ]

    merged_prs = await asyncio.gather(*merged_prs)
    open_prs = await asyncio.gather(*open_prs)
    # stale = await asyncio.gather(*stale)

    return open_prs, merged_prs, stale


async def handle_event(event: str, session):
    contributions = []

    indico_host, indico_id = re.match(r"https://(.*)/event/(\d*)/?", event).groups()
    async with session.get(
        f"https://{indico_host}/export/event/{indico_id}.json?detail=contributions",
    ) as res:
        event = await res.json()

        for contrib in event["results"][0]["contributions"]:
            if contrib["title"] in ("Intro", "Introduction"):
                continue

            start = datetime.datetime.strptime(
                contrib["startDate"]["date"] + " " + contrib["startDate"]["time"],
                "%Y-%m-%d %H:%M:%S",
            )
            contributions.append(
                {
                    "title": contrib["title"],
                    "speakers": [
                        s["first_name"] + " " + s["last_name"]
                        for s in contrib["speakers"]
                    ],
                    "start_date": start,
                    "url": contrib["url"],
                }
            )
    contributions = sorted(contributions, key=lambda c: c["start_date"])
    return contributions


@cli.command(
    help="Generate a LaTeX fragment that includes an overview of PRs, Issues and optionally an Indico agenda"
)
@make_sync
async def generate(
    config: typer.FileText,
    token: str = typer.Option(
        os.environ.get("GH_TOKEN", ...),
        help="Github API token to use. Can be supplied with environment variable GH_TOKEN",
        show_default=False,
    ),
    since: datetime.datetime = typer.Option(
        ...,
        prompt="When was the last meeting? (YYYY-MM-DD)",
        help="Start window for queries",
    ),
    now: datetime.datetime = typer.Option(
        datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
        help="End window for queries",
    ),
    event: Optional[str] = typer.Option(
        None,
        "--event",
        help="Optionally attach an Indico based agenda overview. This only works with public events!",
    ),
    full_tex: bool = typer.Option(
        False, "--full", help="Write a full LaTeX file that is compileable on it's own"
    ),
):

    spec = Spec.parse_obj(yaml.safe_load(config))

    async with aiohttp.ClientSession(loop=asyncio.get_event_loop()) as session:

        if event is not None:
            contributions = handle_event(event, session)

        gh = GitHubAPI(session, __name__, oauth_token=token)

        data = await collect_repositories(spec.repos, gh=gh, since=since, now=now)

        contributions = await contributions if event is not None else []

    print(
        generate_latex(
            spec,
            data,
            since=since,
            now=now,
            contributions=contributions,
            full_tex=full_tex,
        )
    )


@cli.command(help="Print a preamble suitable to render fancy output")
def preamble():
    out = env.loader.get_source(env, "preamble.tex")[0]

    print(out)


@cli.command(help="Print the configuration schema")
def schema():
    print(json.dumps(pydantic.schema.schema([Spec]), indent=2))


@cli.callback()
def main():
    pass


main.__doc__ = """
Meeting generation script, version {version}
""".format(
    version=__version__
)
