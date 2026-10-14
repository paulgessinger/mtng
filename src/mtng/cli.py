import os
import shutil
import subprocess
from tempfile import TemporaryDirectory
from typing import Annotated
import functools
import asyncio
import datetime
from pathlib import Path
import re
import urllib.parse
import json

import typer
from gidgethub.aiohttp import GitHubAPI
import aiohttp
import dateparser
import yaml
import tomllib
from dateutil.tz import tzlocal
from rich.console import Console
from rich.status import Status
from rich import print
import rich.rule

from mtng.generate import generate_latex
from mtng.spec import Spec, IndicoSection
from mtng.collect import collect_repositories
from mtng.sections import collect_sections
from mtng.progress import progress_step
from mtng.report_data import RecordingForge, ReportData
from mtng.forge import GitHubForge
from mtng.forge.http import CachedGitHubAPI, HttpTransport, RateLimitError
from mtng.forge.gitlab import GitLabForge, GitLabError
from mtng.auth import AuthTarget, resolve_token, store_token, validate_token
from mtng.generate import env
from mtng import __version__

console = Console(stderr=True)
cli = typer.Typer(pretty_exceptions_show_locals=False)
auth_cli = typer.Typer(
    help="Authentication helpers", pretty_exceptions_show_locals=False
)


def find_latexmk() -> Path | None:
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


def merge_config(base: dict, overlay: dict) -> dict:
    """Merge mappings recursively; later scalar and list values replace earlier ones."""
    result = dict(base)
    for key, value in overlay.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = merge_config(result[key], value)
        else:
            result[key] = value
    return result


def load_spec(*configs) -> Spec:
    if not configs:
        raise ValueError("Provide a config argument or at least one --config file.")
    merged = {}
    for config in configs:
        name = str(config) if isinstance(config, Path) else config.name
        content = config.read_text() if isinstance(config, Path) else config.read()
        suffix = Path(name).suffix.lower()
        try:
            if suffix == ".toml":
                data = tomllib.loads(content)
            elif suffix == ".json":
                data = json.loads(content)
            else:
                data = yaml.safe_load(content)
        except (ValueError, yaml.YAMLError) as error:
            raise ValueError(f"Invalid config {name}: {error}") from error
        if not isinstance(data, dict):
            raise ValueError(f"Config {name} must contain a mapping at the top level.")
        merged = merge_config(merged, data)
    try:
        return Spec.model_validate(merged)
    except ValueError as error:
        raise ValueError(f"Invalid merged config: {error}") from error


def make_sync(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        asyncio.run(fn(*args, **kwargs))

    return wrapped


def normalize_preamble_content(content: str) -> str:
    # Cheap heuristic: reject files that look like full LaTeX documents.
    uncommented_lines = []
    for line in content.splitlines():
        if line.lstrip().startswith("%"):
            continue
        uncommented_lines.append(line)
    uncommented = "\n".join(uncommented_lines)
    if re.search(r"\\documentclass(?:\s|\[|\{)", uncommented) or re.search(
        r"\\begin\{document\}", uncommented
    ):
        raise typer.BadParameter(
            "--preamble appears to be a full LaTeX document (contains \\documentclass or \\begin{document}); pass a preamble-only file.",
            param_hint="--preamble",
        )
    return content.strip()


def parse_datetime_option(
    value: str | None, param_hint: str
) -> datetime.datetime | None:
    if value is None:
        return None

    parsed = dateparser.parse(value)
    if parsed is None:
        raise typer.BadParameter(
            f"Could not parse date/time value for {param_hint}: {value!r}. Use an ISO timestamp/date or a human-readable value like '1 week ago'.",
            param_hint=param_hint,
        )
    return parsed


def normalize_local_datetime(dt: datetime.datetime) -> datetime.datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=tzlocal())
    return dt.astimezone(tzlocal())


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
    config: Annotated[
        typer.FileText | None,
        typer.Argument(
            help="Base config (or - for stdin); applied before --config files"
        ),
    ] = None,
    configs: Annotated[
        list[Path] | None,
        typer.Option(
            "--config",
            "-c",
            exists=True,
            dir_okay=False,
            help="Config overlay; repeat in order, with later values taking precedence",
        ),
    ] = None,
    save_data: Annotated[
        Path | None, typer.Option(help="Save normalized data for offline rendering")
    ] = None,
    cache_stats: Annotated[
        bool, typer.Option(help="Print HTTP cache and request counters")
    ] = False,
    cache_max_age: Annotated[
        int | None,
        typer.Option(
            min=0,
            help="Override config cache_max_age (seconds; default 300), overriding server freshness",
        ),
    ] = None,
    refresh: Annotated[
        bool, typer.Option(help="Revalidate cached API responses immediately")
    ] = False,
    no_cache: Annotated[
        bool, typer.Option(help="Disable persistent API response caching")
    ] = False,
    token: Annotated[
        str | None,
        typer.Option(
            help="API token for a single host. Falls back to GH_TOKEN/GITLAB_TOKEN or host-specific keychain credentials.",
            show_default=False,
        ),
    ] = None,
    since: Annotated[
        str | None,
        typer.Option(
            help="Start window for queries. Required unless --release is used. Accepts ISO and human-readable values like '1 week ago'.",
        ),
    ] = None,
    now: Annotated[
        str,
        typer.Option(
            help="End window for queries. Accepts ISO and human-readable values like 'now' or 'next monday'.",
        ),
    ] = "now",
    event: Annotated[
        str | None,
        typer.Option(
            "--event",
            help="Optionally attach an Indico based agenda overview. This only works with public events!",
        ),
    ] = None,
    release: Annotated[
        str | None,
        typer.Option(
            "--release",
            help="Summarize merged PRs/MRs from a release tag or release URL instead of a date window.",
        ),
    ] = None,
    full_tex: Annotated[
        bool,
        typer.Option(
            "--full", help="Generate a standalone LaTeX document instead of a fragment"
        ),
    ] = False,
    preamble: Annotated[
        Path | None,
        typer.Option(
            "--preamble",
            dir_okay=False,
            exists=True,
            readable=True,
            help="Prepend this LaTeX preamble file to fragment output. Mutually exclusive with --full.",
        ),
    ] = None,
    pdf: Annotated[
        Path | None,
        typer.Option(
            dir_okay=False,
            help="Compile the report as a PDF file. This requires a LaTeX installation.",
        ),
    ] = None,
    tex: Annotated[
        Path | None,
        typer.Option(
            dir_okay=False,
            help="Write LaTeX to this file instead of stdout (also usable with --pdf)",
        ),
    ] = None,
):
    if preamble is not None and full_tex:
        raise typer.BadParameter(
            "--preamble cannot be used with --full.",
            param_hint="--preamble",
        )

    try:
        spec = load_spec(*([config] if config is not None else []), *(configs or []))
    except ValueError as error:
        raise typer.BadParameter(str(error)) from error
    if cache_max_age is None:
        cache_max_age = spec.cache_max_age
    if spec.sections is not None and event is not None:
        raise typer.BadParameter(
            "Use an indico section instead of --event with explicit sections"
        )
    now = normalize_local_datetime(parse_datetime_option(now, "--now"))
    if since is None:
        if spec.needs_since(release):
            raise typer.BadParameter(
                "--since is required by the configured report sections.",
                param_hint="--since",
            )
        since = now
    else:
        since = normalize_local_datetime(parse_datetime_option(since, "--since"))

    if pdf is not None and preamble is None:
        full_tex = True
        latexmk = find_latexmk()
        if latexmk is None:
            raise ValueError("latexmk could not be found, cannot compile using --pdf")
    elif pdf is not None:
        latexmk = find_latexmk()
        if latexmk is None:
            raise ValueError("latexmk could not be found, cannot compile using --pdf")

    if since > now:
        raise typer.BadParameter("--since must not be after --now")
    keys = {repo.forge_key for repo in spec.source_repositories}
    if token and token.strip() and len(keys) != 1:
        raise typer.BadParameter(
            "--token requires exactly one forge/host in the configuration"
        )
    gitlab_hosts = {host for kind, host in keys if kind == "gitlab"}
    if len(gitlab_hosts) > 1 and os.environ.get("GITLAB_TOKEN", "").strip():
        raise typer.BadParameter(
            "GITLAB_TOKEN is ambiguous with multiple GitLab hosts; use host-specific auth login entries"
        )
    tokens = {}
    for kind, host in sorted(keys):
        tokens[(kind, host)] = resolve_token(
            token,
            AuthTarget(kind, host),
            allow_env=kind == "github" or len(gitlab_hosts) == 1,
        )[0]

    async with aiohttp.ClientSession() as session:
        events = {}
        contributions = []

        forge = {
            (kind, host): (
                GitHubForge(
                    CachedGitHubAPI(
                        session,
                        __name__,
                        oauth_token=value,
                        transport=HttpTransport(
                            session,
                            max_age=0 if refresh else cache_max_age,
                            enabled=not no_cache,
                        ),
                    )
                )
                if kind == "github"
                else GitLabForge(
                    session,
                    host,
                    value,
                    transport=HttpTransport(
                        session,
                        max_age=0 if refresh else cache_max_age,
                        enabled=not no_cache,
                    ),
                )
            )
            for (kind, host), value in tokens.items()
        }

        recorders = (
            {key: RecordingForge(backend, *key) for key, backend in forge.items()}
            if save_data
            else None
        )
        try:
            backends = recorders if recorders is not None else forge
            if spec.sections is None:
                data = await collect_repositories(
                    spec.repos, forge=backends, since=since, now=now, release=release
                )
            else:
                data = await collect_sections(spec, since, now, backends, release)
            if event is not None:
                with progress_step("Fetching Indico agenda") as step:
                    contributions = await handle_event(event, session)
                    step.advance(len(contributions))
            for section in spec.sections or []:
                if isinstance(section, IndicoSection) and section.event not in events:
                    with progress_step(
                        f"Fetching Indico agenda: {section.title}"
                    ) as step:
                        events[section.event] = await handle_event(
                            section.event, session
                        )
                        step.advance(len(events[section.event]))
        except (
            GitLabError,
            RateLimitError,
            aiohttp.ClientError,
            TimeoutError,
        ) as error:
            raise typer.BadParameter(
                f"Repository collection failed: {error}"
            ) from error

        if cache_stats:
            for (kind, host), backend in forge.items():
                transport = (
                    backend.gh.transport if kind == "github" else backend.transport
                )
                stats = transport.stats
                console.print(
                    f"{kind} {host}: {stats['hits']} local cache hits, "
                    f"{stats['requests']} HTTP requests "
                    f"({stats['revalidated']} unchanged), "
                    f"{stats['retries']} retries, {stats['shared']} shared requests"
                )

    if save_data is not None:
        snapshot = ReportData(
            spec=spec,
            since=since,
            now=now,
            release=release,
            contributions=contributions,
            events=events,
            backends=[recorder.data for recorder in recorders.values()],
        )
        save_data.write_text(snapshot.model_dump_json(indent=2) + "\n")
    output_report(
        spec, data, since, now, contributions, full_tex, preamble, pdf, tex, events
    )


def output_report(
    spec,
    data,
    since,
    now,
    contributions,
    full_tex=False,
    preamble=None,
    pdf=None,
    tex=None,
    events=None,
):
    if preamble is not None and full_tex:
        raise typer.BadParameter("--preamble cannot be used with --full.")
    if pdf is not None:
        full_tex = preamble is None
        latexmk = find_latexmk()
        if latexmk is None:
            raise ValueError("latexmk could not be found, cannot compile using --pdf")
    with Status("Generating LaTeX", console=console):
        latex = generate_latex(
            spec,
            data,
            since=since,
            now=now,
            contributions=contributions,
            full_tex=full_tex,
            **({"events": events} if spec.sections is not None else {}),
        )

    if preamble is not None:
        preamble_content = normalize_preamble_content(preamble.read_text())
        if preamble_content == "":
            raise typer.BadParameter(
                "--preamble did not contain any preamble content before \\begin{document}.",
                param_hint="--preamble",
            )
        if pdf is not None:
            latex = (
                "\\documentclass[aspectratio=169,t,13pt,dvipsnames]{beamer}\n\n"
                f"{preamble_content}\n\n"
                "\\begin{document}\n\n"
                f"{latex}\n\n"
                "\\end{document}"
            )
        else:
            latex = f"{preamble_content}\n\n{latex}"

    if tex is not None:
        tex.write_text(latex)
        console.print(f"Wrote LaTeX: {tex}", markup=False, highlight=False)

    if pdf is None:
        if tex is None:
            typer.echo(latex, nl=False)
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
            with Status("Compiling LaTeX", console=console):
                proc = subprocess.Popen(
                    args,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                )
                for line in proc.stdout:
                    console.out(line, end="", highlight=False)
                proc.wait()
                if proc.returncode != 0:
                    raise subprocess.CalledProcessError(proc.returncode, args)
            shutil.copy(d / "source.pdf", pdf)
        console.print(f"Wrote PDF: {pdf}", markup=False, highlight=False)


@cli.command(help="Render saved report data without API calls or credentials")
@make_sync
async def render(
    snapshot: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
    config: Annotated[
        list[Path] | None,
        typer.Option(
            "--config",
            "-c",
            exists=True,
            dir_okay=False,
            help="Override config; repeat to merge files in order. New data requires recollection",
        ),
    ] = None,
    full_tex: Annotated[
        bool,
        typer.Option(
            "--full", help="Generate a standalone LaTeX document instead of a fragment"
        ),
    ] = False,
    preamble: Annotated[Path | None, typer.Option(exists=True, dir_okay=False)] = None,
    pdf: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
    tex: Annotated[Path | None, typer.Option(dir_okay=False)] = None,
):
    try:
        saved = ReportData.model_validate_json(snapshot.read_text())
        spec = saved.spec
        if config:
            spec = load_spec(*config)
        events = {
            url: [item.model_dump() for item in items]
            for url, items in saved.events.items()
        }
        for section in spec.sections or []:
            if isinstance(section, IndicoSection) and section.event not in events:
                raise ValueError(
                    f"Saved data does not contain event {section.event}; collect a new snapshot"
                )
        if spec.sections is None:
            data = await collect_repositories(
                spec.repos,
                saved.since,
                saved.now,
                saved.forges(),
                release=saved.release,
            )
        else:
            data = await collect_sections(
                spec, saved.since, saved.now, saved.forges(), saved.release
            )
    except (ValueError, KeyError) as error:
        raise typer.BadParameter(str(error)) from error
    output_report(
        spec,
        data,
        saved.since,
        saved.now,
        [item.model_dump() for item in saved.contributions],
        full_tex,
        preamble,
        pdf,
        tex,
        events,
    )


@auth_cli.command("login", help="Store an API token in your system keychain")
def login(
    host: Annotated[
        str, typer.Option(help="github.com or a GitLab host")
    ] = "github.com",
    token: Annotated[
        str | None,
        typer.Option(help="API token to store. If omitted, mtng prompts for it."),
    ] = None,
    validate: Annotated[
        bool,
        typer.Option(
            "--validate/--no-validate",
            help="Validate the token against the host before storing it.",
        ),
    ] = True,
):
    target = AuthTarget.from_host(host)
    label = target.label
    if token is None:
        token = typer.prompt(f"{label} API token", hide_input=True)

    token = token.strip()
    if token == "":
        raise typer.BadParameter("Token cannot be empty", param_hint="--token")

    login_name = asyncio.run(validate_token(token, target)) if validate else None
    store_token(token, target)

    if login_name is None:
        print(f"Stored {label} token in your system keychain.")
    else:
        print(f"Stored {label} token for @{login_name} in your system keychain.")


@auth_cli.command("check", help="Validate the configured token against its host")
def check(
    host: Annotated[
        str, typer.Option(help="github.com or a GitLab host")
    ] = "github.com",
    token: Annotated[
        str | None,
        typer.Option(
            help="API token for a single host. Falls back to GH_TOKEN/GITLAB_TOKEN or host-specific keychain credentials.",
            show_default=False,
        ),
    ] = None,
):
    target = AuthTarget.from_host(host)
    resolved_token, _ = resolve_token(token, target)
    login_name = asyncio.run(validate_token(resolved_token, target))
    print(f"{target.label} token is valid for @{login_name}.")


@auth_cli.command(
    "status",
    help="Show where the configured token is loaded from and optionally validate it",
)
def status(
    host: Annotated[
        str, typer.Option(help="github.com or a GitLab host")
    ] = "github.com",
    token: Annotated[
        str | None,
        typer.Option(
            help="API token for a single host. Falls back to GH_TOKEN/GITLAB_TOKEN or host-specific keychain credentials.",
            show_default=False,
        ),
    ] = None,
    validate: Annotated[
        bool,
        typer.Option(
            "--validate/--no-validate",
            help="Validate the token against its host.",
        ),
    ] = False,
):
    target = AuthTarget.from_host(host)
    resolved_token, source = resolve_token(token, target)
    print(f"{target.label} token is configured (source: {source}).")
    if validate:
        login_name = asyncio.run(validate_token(resolved_token, target))
        print(f"{target.label} token is valid for @{login_name}.")


@cli.command(help="Print a preamble suitable to render fancy output")
def preamble():
    out = env.loader.get_source(env, "preamble.tex")[0]

    # typer.echo, not rich's print: this output is meant to be redirected into a
    # file, and rich would consume '[...]' as style markup and wrap long lines.
    typer.echo(out)


@cli.command(help="Print the configuration schema")
def schema():
    typer.echo(json.dumps(Spec.model_json_schema(), indent=2))


cli.add_typer(auth_cli, name="auth")


@cli.callback()
def main():
    pass


main.__doc__ = """
Meeting generation script, version {version}
""".format(version=__version__)
