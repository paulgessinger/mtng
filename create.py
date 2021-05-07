#!/usr/bin/env python3

from dotenv import load_dotenv
load_dotenv()

from github import Github
from datetime import datetime, timedelta

import os

g = Github(os.environ["GH_TOKEN"])


dt = datetime.now() - timedelta(days=14)
for repo in (
    "acts-project/traccc",
    "acts-project/vecmem"
):
    issues = g.search_issues(f"repo:{repo} is:pr merged:>={dt.strftime('%Y-%m-%d')}")

    print(repo)
    prs = [i.as_pull_request() for i in issues]
    prs = sorted(prs, key=lambda p: p.merged_at)
    for pr in prs:
        s = f"#{pr.number} - {pr.title} by @{pr.user.login}, merged on {pr.merged_at.strftime('%Y-%m-%d')}"
        print(s)
        # print(pr, pr.merged_at)
