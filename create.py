#!/usr/bin/env python3

from jinja2.loaders import FileSystemLoader
from dotenv import load_dotenv
from github import Github
import requests

from datetime import datetime, timedelta
from pathlib import Path
import os
import sys

load_dotenv()
g = Github(os.environ["GH_TOKEN"])


dt = datetime.now() - timedelta(days=14)

data = {}

r = requests.get("https://indico.cern.ch/export/event/1035950.json?detail=contributions")

event = r.json()

# print(event["results"][0]["contributions"])

# sys.exit()


for repo in (
    "acts-project/traccc",
    "acts-project/vecmem"
):
    issues = g.search_issues(f"repo:{repo} is:pr merged:>={dt.strftime('%Y-%m-%d')}")

    # data.setdefault(repo, [])

    prs = [i.as_pull_request() for i in issues]
    # prs = sorted(prs, key=lambda p: p.merged_at)
    data[repo] = prs
    # for pr in prs:
        # s = f"#{pr.number} - {pr.title} by @{pr.user.login}, merged on {pr.merged_at.strftime('%Y-%m-%d')}"

        # print(s)
        # print(pr, pr.merged_at)
        # data[repo].append(pr)


from jinja2 import Environment, FileSystemLoader
env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "template"),
)

def sanitize(s):
  return s.replace("_", "\\_").replace("#", "\\#")
  
env.filters["sanitize"] = sanitize

tpl = env.get_template("main.tex")

contributions = []

for contrib in event["results"][0]["contributions"]:
  start = datetime.strptime(contrib["startDate"]["date"]+ " " + contrib["startDate"]["time"], "%Y-%m-%d %H:%M:%S")
  contributions.append({
    "title": contrib["title"],
    "speakers": [s["first_name"] + " " + s["last_name"] for s in contrib["speakers"]],
    "start_date": start
  })

contributions = sorted(contributions, key=lambda c: c["start_date"])

print(tpl.render(
  repos=data,
  last=dt,
  contributions=contributions
))