from typing import List
from datetime import datetime
import urllib.parse
import asyncio
import dateutil.parser

from gidgethub.abc import GitHubAPI

from mtng.spec import Repository

async def collect_repositories(repos: List[Repository], since: datetime, now: datetime, gh: GitHubAPI):
    data = {}
    for repo in repos:
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
                        dateutil.parser.parse(pr[k]).replace(tzinfo=None) if pr[k] is not None else None
                    )

    return data
