"""Generate a small review-follow-up deck with fictional PRs.

Run: uv run python examples/review_followup.py > /tmp/review-followup.tex
Compile the output with latexmk -pdf.
"""

import asyncio
from datetime import datetime, timedelta, timezone

from mtng.sections import collect_sections
from mtng.generate import generate_latex
from mtng.models import PullRequest, Review, User
from mtng.spec import Repository, Spec


async def main():
    now = datetime(2026, 11, 2, tzinfo=timezone.utc)
    alice = User(login="alice", html_url="https://example.com/alice")
    bob = User(login="bob", html_url="https://example.com/bob")
    author = User(login="contributor", html_url="https://example.com/contributor")
    rows = [
        (142, "Handle expired authentication tokens", 12, [], []),
        (151, "Document the offline report workflow", 3, [], []),
        (
            137,
            "Preserve labels when replaying reports",
            18,
            [alice, bob],
            [Review(user=bob, state="COMMENTED")],
        ),
        (148, "Improve retry handling for API requests", 6, [alice], []),
        (
            160,
            "Address review feedback",
            7,
            [],
            [Review(user=alice, state="CHANGES_REQUESTED")],
        ),
        (161, "Merge approved update", 4, [], [Review(user=alice, state="APPROVED")]),
        (
            162,
            "Clarify review discussion",
            2,
            [],
            [Review(user=alice, state="COMMENTED")],
        ),
        (
            163,
            "Check updates after review comments",
            5,
            [],
            [Review(user=alice, state="COMMENTED", commit_id="old-head")],
        ),
        (145, "Add pagination to the activity summary", 9, [bob], []),
    ]
    prs = [
        PullRequest(
            number=number,
            title=title,
            created_at=now - timedelta(days=age),
            updated_at=now,
            user=author,
            assignees=assignees,
            reviews=reviews,
            head_sha="current-head",
            labels=[],
            draft=False,
            additions=number * 3,
            deletions=number // 4,
            changed_files=number % 5 + 1,
            html_url=f"https://example.com/project/pull/{number}",
            url="https://example.com/api",
        )
        for number, title, age, assignees, reviews in rows
    ]

    class ExampleForge:
        async def open_prs(self, *args, **kwargs):
            return prs

    repo = Repository(
        name="example/project",
        display_name="Example project",
        do_review_followup=True,
        do_open_prs=False,
        do_merged_prs=False,
    )
    spec = Spec(
        repositories={"project": repo},
        sections=[{"type": "review_followup", "repositories": ["project"]}],
        title="Review follow-up",
        footline_left="Fictional example",
    )
    data = await collect_sections(spec, now - timedelta(days=7), now, ExampleForge())
    tex = generate_latex(spec, data, now - timedelta(days=7), now, [], True)
    # Keep the example focused on the follow-up slides.
    print(tex.replace(r"\frame[plain]{\titlepage}", ""))


if __name__ == "__main__":
    asyncio.run(main())
