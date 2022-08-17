from typing import List, Optional, Literal
from datetime import datetime
import urllib.parse
import asyncio
import dateutil.parser

from gidgethub.abc import GitHubAPI

from mtng.spec import Repository


async def get_merged_pulls(
    gh: GitHubAPI, repo_name: str, start: datetime, end: datetime
):
    return await asyncio.gather(
        *[
            gh.getitem(f"/repos/{repo_name}/pulls/{issue['number']}")
            async for issue in gh.getiter(
                f"/search/issues?q=repo:{repo_name}+is:pr+merged:{start:%Y-%m-%d}..{end:%Y-%m-%d}",
            )
        ]
    )


async def get_open_issues(
    gh: GitHubAPI,
    repo_name: str,
    with_labels: List[str] = [],
    without_labels: List[str] = [],
    start: Optional[datetime] = None,
    end: Optional[datetime] = None,
    type: Literal["pr", "issue", "any"] = "issue",
):
    if not all([start is None, end is None]) and not all(
        [start is not None, end is not None]
    ):
        raise ValueError("Either provide start and end or neither")
    url = f"/search/issues?q=repo:{repo_name}+is:open"
    if type != "any":
        url += f"+is:{type}"
    if start is not None and end is not None:
        url += f"+created:{start:%Y-%m-%d}..{end:%Y-%m-%d}"
    for label in without_labels:
        url += f'+-label:"{urllib.parse.quote(label)}"'
    for label in with_labels:
        url += f'+label:"{urllib.parse.quote(label)}"'
    return await asyncio.gather(
        *[
            gh.getitem(f"/repos/{repo_name}/issues/{issue['number']}")
            async for issue in gh.getiter(url)
        ]
    )


async def collect_repositories(
    repos: List[Repository], since: datetime, now: datetime, gh: GitHubAPI
):
    data = {}
    for repo in repos:
        data[repo.name] = {}
        data[repo.name]["merged_prs"] = []
        data[repo.name]["open_prs"] = []
        data[repo.name]["stale"] = []
        data[repo.name]["recent_issues"] = []
        data[repo.name]["spec"] = repo

        if repo.do_merged_prs:
            merged_prs = await get_merged_pulls(gh, repo.name, since, now)
            data[repo.name]["merged_prs"] = merged_prs

        if repo.do_open_prs:
            open_prs = await get_open_issues(
                gh,
                repo.name,
                without_labels=repo.filter_labels,
                type="pr",
            )

            if not repo.show_wip:
                open_prs = list(filter(lambda pr: not pr["is_wip"], open_prs))
            data[repo.name]["open_prs"] = open_prs

        if repo.do_stale:
            if repo.stale_label is None:
                raise ValueError("Provide stale label if do_stale=True")
            stale = await get_open_issues(
                gh, repo.name, with_labels=[repo.stale_label], type="any"
            )

            data[repo.name]["stale"] = stale

        if repo.do_recent_issues:
            recent_issues = await get_open_issues(gh, repo.name, start=since, end=now)

            data[repo.name]["recent_issues"] = recent_issues

        for prk in "open_prs", "merged_prs", "stale", "recent_issues":
            for pr in data[repo.name][prk]:

                pr["is_wip"] = repo.wip_label in [l["name"] for l in pr["labels"]]
                pr["is_stale"] = repo.stale_label in [l["name"] for l in pr["labels"]]

                for k in "merged_at", "updated_at":
                    if not k in pr:
                        continue
                    pr[k] = (
                        dateutil.parser.parse(pr[k]).replace(tzinfo=None)
                        if pr[k] is not None
                        else None
                    )
    return data
