#!/usr/bin/env python3

from jinja2.loaders import FileSystemLoader
from dotenv import load_dotenv
from github import Github
import requests
import typer

from datetime import datetime, timedelta
from pathlib import Path
import os
import sys
import re
from concurrent.futures import ThreadPoolExecutor

load_dotenv()

def main(
  indico: str
):
  g = Github(os.environ["GH_TOKEN"])


  # dt = datetime.now() - timedelta(days=)
  dt = datetime(year=2021, month=5, day=7)

  data = {}

  indico_id = re.match(r"https://indico.cern.ch/event/(\d*)/?", indico).group(1)

  r = requests.get(f"https://indico.cern.ch/export/event/{indico_id}.json?detail=contributions")

  event = r.json()

  # print(event["results"][0]["contributions"])

  # sys.exit()

  with ThreadPoolExecutor() as ex:

    for repo in (
        "acts-project/traccc",
        "acts-project/vecmem",
        "acts-project/detray"
    ):
        data[repo] = {}
        issues = g.search_issues(f"repo:{repo} is:pr merged:>={dt.strftime('%Y-%m-%d')}")
        merged_prs = ex.map(lambda i: i.as_pull_request(), issues)
        data[repo]["merged_prs"] = merged_prs

        r = g.get_repo(repo)

        prs = r.get_pulls(state="opened")
        data[repo]["open_prs"] = prs

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
      if contrib["title"] in ("Intro", "Introduction"):
        continue

      start = datetime.strptime(contrib["startDate"]["date"]+ " " + contrib["startDate"]["time"], "%Y-%m-%d %H:%M:%S")
      contributions.append({
        "title": contrib["title"],
        "speakers": [s["first_name"] + " " + s["last_name"] for s in contrib["speakers"]],
        "start_date": start,
        "url": contrib["url"],
      })

    contributions = sorted(contributions, key=lambda c: c["start_date"])

    print(tpl.render(
      repos=data,
      last=dt,
      contributions=contributions
    ))

if "__main__" == __name__:
  typer.run(main)