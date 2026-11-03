from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from fake_forge import FakeForge
from mtng.collect import collect_repositories
from mtng.generate import generate_latex
from mtng.models import Label, PullRequest, Review, User
from mtng.reviews import review_followup
from mtng.sections import collect_sections
from mtng.spec import Repository, Spec

NOW = datetime(2026, 11, 2, tzinfo=timezone.utc)
ALICE = User(login="alice", html_url="https://example.com/alice")
BOB = User(login="bob", html_url="https://example.com/bob")


def pr(number=1, **kwargs):
    return PullRequest(
        number=number,
        title="Fix cache & retry handling",
        user=BOB,
        labels=kwargs.pop("labels", []),
        url="https://example.com/api",
        html_url=f"https://example.com/pull/{number}",
        created_at=NOW - timedelta(days=number),
        updated_at=kwargs.pop("updated_at", NOW),
        **kwargs,
    )


def test_assignments_drafts_and_oldest_first():
    groups = review_followup(
        [
            pr(1),
            pr(4),
            pr(2, draft=True),
            pr(3, labels=[Label(name="wip")]),
            pr(
                5,
                assignees=[ALICE, BOB],
                reviews=[Review(user=ALICE, state="COMMENTED")],
            ),
            pr(6, assignee=ALICE, reviews=[Review(user=BOB, state="APPROVED")]),
        ],
        "wip",
    )
    assert [(g["title"], [p.number for p in g["items"]]) for g in groups] == [
        ("Needs assignment", [4, 1]),
        ("Awaiting review: alice", [6]),
        ("Awaiting review: bob", [5]),
    ]


@pytest.mark.parametrize(
    "state,cleared",
    [
        ("APPROVED", True),
        ("CHANGES_REQUESTED", True),
        ("COMMENTED", True),
        ("PENDING", False),
        ("DISMISSED", False),
    ],
)
def test_review_participation(state, cleared):
    assert (
        bool(
            review_followup(
                [pr(assignee=ALICE, reviews=[Review(user=ALICE, state=state)])]
            )
        )
        != cleared
    )


def test_unknown_and_partial_review_data():
    groups = review_followup(
        [
            pr(1, assignee=ALICE, reviews_available=False),
            pr(
                2,
                assignees=[ALICE, BOB],
                review_activity_available=False,
                reviews=[Review(user=BOB, state="APPROVED")],
            ),
        ]
    )
    assert len(groups) == 1
    assert groups[0]["title"] == "Review status unknown: alice"
    assert [p.number for p in groups[0]["items"]] == [2, 1]


@pytest.mark.asyncio
async def test_followup_collects_reviews_without_open_pr_display():
    repo = Repository(
        name="example/project",
        do_review_followup=True,
        do_open_prs=False,
        do_merged_prs=False,
        do_reviewers=False,
    )
    forge = FakeForge(open_prs=[pr(), pr(2, assignees=[ALICE])])
    forge.open_prs = AsyncMock(wraps=forge.open_prs)
    data = await collect_repositories([repo], NOW - timedelta(days=7), NOW, forge)
    assert forge.open_prs.call_args.kwargs["requirements"].reviews
    assert data[repo.name]["open_prs"] == []
    tex = generate_latex(Spec(repos=[repo]), data, NOW, NOW, [], False)
    assert "Needs assignment" in tex
    assert "Awaiting review: alice" in tex
    assert r"Fix cache \& retry handling" in tex
    assert "No updates for 0 days; PR age: 2 days" in tex


@pytest.mark.asyncio
async def test_ordered_section_and_all_clear():
    spec = Spec.model_validate(
        {
            "repositories": {"project": {"name": "example/project"}},
            "sections": [
                {
                    "type": "review_followup",
                    "repositories": ["project"],
                    "title": "Review check-in",
                }
            ],
        }
    )
    data = await collect_sections(
        spec,
        NOW,
        NOW,
        FakeForge(
            open_prs=[
                pr(assignee=ALICE, reviews=[Review(user=ALICE, state="APPROVED")])
            ]
        ),
    )
    tex = generate_latex(spec, data, NOW, NOW, [], False)
    assert "Review check-in" in tex
    assert "All non-draft PRs have a review assignment" in tex


@pytest.mark.parametrize("assigned", [False, True])
def test_longest_inactive_first_even_when_pr_is_newer(assigned):
    kwargs = {"assignees": [ALICE]} if assigned else {}
    groups = review_followup(
        [
            pr(30, updated_at=NOW, **kwargs),
            pr(10, updated_at=NOW - timedelta(days=7), **kwargs),
            pr(20, updated_at=NOW - timedelta(days=2), **kwargs),
        ]
    )
    assert [p.number for p in groups[0]["items"]] == [10, 20, 30]


def test_requested_reviewers_count_as_assignment_and_are_tracked_individually():
    groups = review_followup(
        [
            pr(1, requested_reviewers=[ALICE]),
            pr(
                2,
                assignees=[ALICE],
                requested_reviewers=[ALICE, BOB],
                reviews=[Review(user=ALICE, state="COMMENTED")],
            ),
            pr(3, assignees=[ALICE], requested_reviewers=[ALICE]),
        ]
    )
    assert [(g["title"], [p.number for p in g["items"]]) for g in groups] == [
        ("Awaiting review: alice", [3, 1]),
        ("Awaiting review: bob", [2]),
    ]


def test_requested_reviewers_with_unavailable_reviews_are_unknown():
    groups = review_followup([pr(requested_reviewers=[ALICE], reviews_available=False)])
    assert groups[0]["title"] == "Review status unknown: alice"


@pytest.mark.parametrize(
    "source,expected",
    [
        ("assignees", ["Needs assignment", "Awaiting review: alice"]),
        ("requested_reviewers", ["Awaiting review: bob"]),
        ("both", ["Awaiting review: alice", "Awaiting review: bob"]),
    ],
)
def test_configurable_assignment_source(source, expected):
    groups = review_followup(
        [
            pr(1, assignees=[ALICE], requested_reviewers=[BOB]),
            pr(2, requested_reviewers=[BOB]),
        ],
        assignment_source=source,
    )
    assert [g["title"] for g in groups] == expected


@pytest.mark.asyncio
async def test_section_assignment_source_overrides_repository():
    spec = Spec.model_validate(
        {
            "repositories": {
                "project": {
                    "name": "example/project",
                    "review_assignment_source": "assignees",
                }
            },
            "sections": [
                {
                    "type": "review_followup",
                    "repositories": ["project"],
                    "assignment_source": "requested_reviewers",
                }
            ],
        }
    )
    data = await collect_sections(
        spec,
        NOW,
        NOW,
        FakeForge(open_prs=[pr(assignees=[ALICE], requested_reviewers=[BOB])]),
    )
    assert [g["title"] for g in next(iter(data.values()))["review_followup"]] == [
        "Awaiting review: bob"
    ]
