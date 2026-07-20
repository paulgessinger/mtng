import os
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Optional, List
import functools
import asyncio
import datetime
from pathlib import Path
import re
import urllib.parse
import json

import typer
import keyring
from gidgethub.aiohttp import GitHubAPI
import gidgethub
import aiohttp
import dateutil.parser
import yaml
from keyring.errors import KeyringError
from dateutil.tz import tzlocal
from rich.status import Status
from rich import print
from rich.panel import Panel
import rich.rule

from mtng.generate import generate_latex
from mtng.spec import Spec
from mtng.collect import collect_repositories
from mtng.generate import env
from mtng import __version__

cli = typer.Typer()
auth_cli = typer.Typer(help="Authentication helpers")
KEYRING_SERVICE = "mtng"
KEYRING_USERNAME = "github-token"


def find_latexmk() -> Path:
    try:
        latexmk_path = Path(
            subprocess.check_output(["which", "latexmk"]).decode().strip()
        )
    except subprocess.CalledProcessError:
        return None
    if not latexmk_path.exists():
        return None
    return latexmk_path


def have_lualatex() -> bool:
    try:
        latexmk_path = Path(
            subprocess.check_output(["which", "lualatex"]).decode().strip()
        )
    except subprocess.CalledProcessError:
        return False
    if not latexmk_path.exists():
        return False
    return True


def make_sync(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))

    return wrapped


def get_keyring_token() -> Optional[str]:
    try:
        token = keyring.get_password(KEYRING_SERVICE, KEYRING_USERNAME)
    except KeyringError as e:
        raise typer.BadParameter(
            f"Unable to access the system keychain: {e}",
            param_hint="--token",
        ) from e
    if token is None:
        return None
    token = token.strip()
    return token or None


def resolve_github_token(token: Optional[str]) -> str:
    token_value, _ = resolve_github_token_with_source(token)
    return token_value


def resolve_github_token_with_source(token: Optional[str]) -> tuple[str, str]:
    if token is not None:
        token = token.strip()
        if token != "":
            return token, "--token"

    env_token = os.environ.get("GH_TOKEN")
    if env_token is not None:
        env_token = env_token.strip()
        if env_token != "":
            return env_token, "GH_TOKEN"

    keyring_token = get_keyring_token()
    if keyring_token is not None:
        return keyring_token, "system keychain"

    raise typer.BadParameter(
        "No GitHub token provided. Use --token, GH_TOKEN, or run `mtng auth login`.",
        param_hint="--token",
    )


async def validate_github_token(token: str) -> str:
    async with aiohttp.ClientSession() as session:
        gh = GitHubAPI(session, __name__, oauth_token=token)
        try:
            user = await gh.getitem("/user")
        except gidgethub.BadRequest as e:
            raise typer.BadParameter(
                f"GitHub token validation failed: {format_github_request_error(e)}",
                param_hint="--token",
            ) from e
    return user["login"]


def validate_github_token_sync(token: str) -> str:
    return asyncio.run(validate_github_token(token))


def format_github_request_error(e: Exception) -> str:
    status_code = getattr(e, "status_code", None)
    detail = str(e).strip() or "Unknown error"
    if status_code is None:
        return detail
    return f"{status_code} {detail}"


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
    token: Optional[str] = typer.Option(
        None,
        help="Github API token. Falls back to GH_TOKEN or a token stored via `mtng auth login`.",
        show_default=False,
    ),
    since: Optional[datetime.datetime] = typer.Option(
        None,
        help="Start window for queries. Required unless --release is used.",
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
    release: Optional[str] = typer.Option(
        None,
        "--release",
        help="Summarize merged PRs from a GitHub release tag or release URL instead of a date window.",
    ),
    full_tex: bool = typer.Option(
        False, "--full", help="Write a full LaTeX file that is compileable on it's own"
    ),
    pdf: Optional[Path] = typer.Option(
        None,
        dir_okay=False,
        help="Compile the report as a PDF file. This requires a LaTeX installation.",
    ),
    tex: Optional[Path] = typer.Option(
        None, dir_okay=False, help="Write LaTex output to this file"
    ),
):
    token = resolve_github_token(token)
    now = now.replace(tzinfo=tzlocal())
    if since is None:
        if release is None:
            raise typer.BadParameter(
                "--since is required unless --release is provided.",
                param_hint="--since",
            )
        since = now
    else:
        since = since.replace(tzinfo=tzlocal())

    if pdf is not None:
        full_tex = True
        latexmk = find_latexmk()
        if latexmk is None:
            raise ValueError("latexmk could not be found, cannot compile using --pdf")

    spec = Spec.model_validate(yaml.safe_load(config))

    async with aiohttp.ClientSession() as session:
        if event is not None:
            contributions = handle_event(event, session)

        gh = GitHubAPI(session, __name__, oauth_token=token)

        print(Panel("Collection data from GitHub"))
        data = await collect_repositories(
            spec.repos, gh=gh, since=since, now=now, release=release
        )

        contributions = await contributions if event is not None else []

    with Status("Generating LaTeX"):
        latex = generate_latex(
            spec,
            data,
            since=since,
            now=now,
            contributions=contributions,
            full_tex=full_tex,
        )

    if pdf is None:
        if tex is not None:
            tex.write_text(latex)
        print(Panel(latex, title="LaTeX Output"))
    else:
        with TemporaryDirectory() as d:
            d = Path(d)
            source = d / "source.tex"
            source.write_text(latex)
            args = [
                latexmk,
                f"-output-directory={d}",
                "-halt-on-error",
                "-pdf",
            ]

            if have_lualatex():
                args.append("-pdflatex=lualatex")
            args.append(source)
            with Status("Compiling LaTeX"):
                subprocess.check_call(args)
            shutil.copy(d / "source.pdf", pdf)


@auth_cli.command("login", help="Store a GitHub API token in your system keychain")
def login(
    token: Optional[str] = typer.Option(
        None,
        help="GitHub API token to store. If omitted, mtng prompts for it.",
    ),
    validate: bool = typer.Option(
        True,
        "--validate/--no-validate",
        help="Validate the token against GitHub before storing it.",
    ),
):
    if token is None:
        token = typer.prompt("GitHub API token", hide_input=True)

    token = token.strip()
    if token == "":
        raise typer.BadParameter("Token cannot be empty", param_hint="--token")

    login_name = validate_github_token_sync(token) if validate else None

    try:
        keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, token)
    except KeyringError as e:
        raise typer.BadParameter(
            f"Unable to store token in the system keychain: {e}",
            param_hint="--token",
        ) from e

    if login_name is None:
        print("Stored GitHub token in your system keychain.")
    else:
        print(f"Stored GitHub token for @{login_name} in your system keychain.")


@auth_cli.command("check", help="Validate the configured GitHub token against the GitHub API")
def check(
    token: Optional[str] = typer.Option(
        None,
        help="Github API token. Falls back to GH_TOKEN or a token stored via `mtng auth login`.",
        show_default=False,
    ),
):
    resolved_token = resolve_github_token(token)
    login_name = validate_github_token_sync(resolved_token)
    print(f"GitHub token is valid for @{login_name}.")


@auth_cli.command(
    "status",
    help="Show where the configured GitHub token is loaded from and optionally validate it",
)
def status(
    token: Optional[str] = typer.Option(
        None,
        help="Github API token. Falls back to GH_TOKEN or a token stored via `mtng auth login`.",
        show_default=False,
    ),
    validate: bool = typer.Option(
        False,
        "--validate/--no-validate",
        help="Validate the token against GitHub.",
    ),
):
    _, source = resolve_github_token_with_source(token)
    print(f"GitHub token is configured (source: {source}).")
    if validate:
        resolved_token = resolve_github_token(token)
        login_name = validate_github_token_sync(resolved_token)
        print(f"GitHub token is valid for @{login_name}.")


@cli.command(help="Print a preamble suitable to render fancy output")
def preamble():
    out = env.loader.get_source(env, "preamble.tex")[0]

    print(out)


@cli.command(help="Print the configuration schema")
def schema():
    print(json.dumps(Spec.model_json_schema(), indent=2))


cli.add_typer(auth_cli, name="auth")


@cli.callback()
def main():
    pass


main.__doc__ = """
Meeting generation script, version {version}
""".format(version=__version__)
