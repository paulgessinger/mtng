import os
from typing import Optional, List
import functools
import asyncio
import datetime
from pathlib import Path
import re
import urllib.parse

import typer
from dotenv import load_dotenv
from gidgethub.aiohttp import GitHubAPI
import gidgethub
import aiohttp
import dateutil.parser
import yaml

from mtng.spec import Spec
from mtng import __version__

load_dotenv(dotenv_path=Path.cwd() /".env")

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

    indico_id = re.match(r"https://indico.cern.ch/event/(\d*)/?", event).group(1)
    async with session.get(
        f"https://indico.cern.ch/export/event/{indico_id}.json?detail=contributions",
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
        os.environ.get("GH_TOKEN",...),
        help="Github API token to use. Can be supplied with environment variable GH_TOKEN",
    ),
    since: datetime.datetime = typer.Option(
        ..., prompt="When was the last meeting? (YYYY-MM-DD)"
    ),
    now: datetime.datetime = typer.Option(
        datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    ),
    event: Optional[str] = typer.Option(None, "--event"),
):

    spec = Spec.parse_obj(yaml.safe_load(config))

    async with aiohttp.ClientSession(loop=asyncio.get_event_loop()) as session:

        if event is not None:
            contributions = handle_event(event, session)

        gh = GitHubAPI(session, __name__, oauth_token=token)
        data = {}
        for repo in spec.repos:
            # open_prs, merged_prs, stale = await collect(gh, repo, dt)
            data[repo.name] = {}
            data[repo.name]["merged_prs"] = []
            data[repo.name]["open_prs"] = []
            data[repo.name]["stale"] = []
            data[repo.name]["recent_issues"] = []
            data[repo.name]["spec"] = repo

            if repo.do_merged_prs:
                merged_prs = [
                    gh.getitem(f"/repos/{repo.name}/pulls/{issue['number']}")
                    async for issue in gh.getiter(
                        f"/search/issues?q=repo:{repo.name}+is:pr+merged:{since:%Y-%m-%d}..{now:%Y-%m-%d}",
                    )
                ]
                data[repo.name]["merged_prs"] = merged_prs

            if repo.do_open_prs:
                url = f"/search/issues?q=repo:{repo.name}+is:pr+is:open"
                if repo.wip_label is not None:
                    url += f'+-label:"{urllib.parse.quote(repo.wip_label)}"'
                    # url += '+-label:"{repo.wip_label}"'
                open_prs = [
                    gh.getitem(f"/repos/{repo.name}/pulls/{issue['number']}")
                    async for issue in gh.getiter(url)
                ]
                data[repo.name]["open_prs"] = open_prs

            if repo.do_stale:
                stale = [
                    issue
                    async for issue in gh.getiter(
                        f'/search/issues?q=repo:{repo.name}+is:open+label:"{urllib.parse.quote(repo.stale_label)}"'
                    )
                ]

                data[repo.name]["stale"] = stale

            if repo.do_recent_issues:
                url = f"/search/issues?q=repo:{repo.name}+is:open+is:issue+created:{since:%Y-%m-%d}..{now:%Y-%m-%d}"
                #  print(url)
                recent_issues = [issue async for issue in gh.getiter(url)]
                data[repo.name]["recent_issues"] = recent_issues

            for prk in "open_prs", "merged_prs":
                data[repo.name][prk] = await asyncio.gather(*data[repo.name][prk])

            for prk in "open_prs", "merged_prs", "stale", "recent_issues":
                for pr in data[repo.name][prk]:
                    for k in "merged_at", "updated_at":
                        if not k in pr:
                            continue
                        pr[k] = (
                            dateutil.parser.parse(pr[k]) if pr[k] is not None else None
                        )

        contributions = await contributions if event is not None else []

    from jinja2 import Environment, FileSystemLoader

    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "template"),
    )

    def sanitize(s):
        return s.replace("_", "\\_").replace("#", "\\#").replace("&", "\\&")

    env.filters["sanitize"] = sanitize

    tpl = env.get_template("main.tex")

    print(tpl.render(repos=data, spec=spec, last=since, contributions=contributions))


@cli.callback()
def main():
    pass


main.__doc__ = """
Meeting generation script, version {version}
""".format(version=__version__)

