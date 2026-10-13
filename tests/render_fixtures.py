"""Generate CI compilation inputs without API access or credentials."""

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from fake_forge import FakeForge
from mtng.collect import collect_repositories
from mtng.generate import generate_latex
from mtng.models import Issue, PullRequest
from mtng.spec import Repository, Spec


async def main():
    ref = Path(__file__).parent / "ref"

    def read(name, cls):
        return [
            cls.model_validate(item) for item in json.loads((ref / name).read_text())
        ]

    repo = Repository(
        name="acts-project/acts", stale_label="Stale", do_recent_issues=True
    )
    forge = FakeForge(
        merged_prs=read("merged_prs.json", PullRequest),
        open_prs=read("open_prs.json", Issue),
        labelled={"Stale": read("stale.json", Issue)},
        recent_issues=read("recent_issues.json", Issue),
    )
    since = datetime(2022, 8, 1, tzinfo=timezone.utc)
    now = datetime(2022, 8, 17, tzinfo=timezone.utc)
    data = await collect_repositories([repo], since, now, forge)
    for full in (False, True):
        Path("full.tex" if full else "fragment.tex").write_text(
            generate_latex(Spec(repos=[repo]), data, since, now, [], full)
        )


if __name__ == "__main__":
    asyncio.run(main())
