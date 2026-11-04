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
    "state,expected",
    [
        ("APPROVED", "Needs maintainer attention (approved)"),
        ("CHANGES_REQUESTED", "Awaiting author: bob"),
        ("COMMENTED", "Review comments, no decision"),
        ("PENDING", "Awaiting review: alice"),
        ("DISMISSED", "Awaiting review: alice"),
    ],
)
def test_review_participation(state, expected):
    groups = review_followup(
        [pr(assignee=ALICE, reviews=[Review(user=ALICE, state=state)])]
    )
    assert [g["title"] for g in groups] == [expected]


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
    assert len(groups) == 2
    assert groups[1]["title"] == "Review status unknown: bob"
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
    assert (
        r"Awaiting review: \href{https://example.com/alice}{\mtngusername{{@}{a}{l}{i}{c}{e}}}"
        in tex
    )
    from mtng.generate import title_units

    assert str(title_units("Fix cache & retry handling")) in tex
    assert "open for 2 days" in tex
    assert "Likely next action" not in tex


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
    assert "Needs maintainer attention (approved)" in tex


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


@pytest.mark.parametrize(
    "policy,expected", [("any", None), ("all", ["Awaiting review: bob"])]
)
@pytest.mark.parametrize("state", ["APPROVED", "CHANGES_REQUESTED", "COMMENTED"])
def test_review_completion_policy(policy, expected, state):
    if state == "CHANGES_REQUESTED":
        expected = ["Awaiting author: bob"]
    elif policy == "any":
        expected = [
            (
                "Needs maintainer attention (approved)"
                if state == "APPROVED"
                else "Review comments, no decision"
            )
        ]
    groups = review_followup(
        [pr(assignees=[ALICE, BOB], reviews=[Review(user=ALICE, state=state)])],
        review_completion=policy,
    )
    assert [g["title"] for g in groups] == expected


@pytest.mark.parametrize("state", ["PENDING", "DISMISSED"])
def test_any_completion_requires_submitted_review(state):
    groups = review_followup(
        [
            pr(
                requested_reviewers=[ALICE, BOB],
                reviews=[Review(user=ALICE, state=state)],
            )
        ],
        review_completion="any",
    )
    assert len(groups) == 2


def test_any_completion_counts_former_reviewers_and_preserves_unknown_data():
    groups = review_followup(
        [
            pr(
                1,
                assignees=[ALICE],
                requested_reviewers=[BOB],
                reviews=[Review(user=ALICE, state="APPROVED")],
            ),
            pr(2, requested_reviewers=[BOB], reviews_available=False),
            pr(
                3,
                requested_reviewers=[ALICE, BOB],
                review_activity_available=False,
                reviews=[Review(user=ALICE, state="APPROVED")],
            ),
            pr(4),
        ],
        assignment_source="requested_reviewers",
        review_completion="any",
    )
    assert [(g["title"], [p.number for p in g["items"]]) for g in groups] == [
        ("Needs assignment", [4]),
        ("Needs maintainer attention (approved)", [1]),
        ("Review status unknown: bob", [3, 2]),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "repository_policy,section_policy,expected",
    [
        ("any", None, ["Needs maintainer attention (approved)"]),
        ("all", "any", ["Needs maintainer attention (approved)"]),
        ("any", "all", ["Awaiting review: bob"]),
    ],
)
async def test_section_review_completion_inheritance(
    repository_policy, section_policy, expected
):
    spec = Spec.model_validate(
        {
            "repositories": {
                "project": {
                    "name": "example/project",
                    "review_completion": repository_policy,
                }
            },
            "sections": [
                {
                    "type": "review_followup",
                    "repositories": ["project"],
                    "review_completion": section_policy,
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
                pr(
                    assignees=[ALICE, BOB],
                    reviews=[Review(user=ALICE, state="APPROVED")],
                )
            ]
        ),
    )
    assert [
        g["title"] for g in next(iter(data.values()))["review_followup"]
    ] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("size,files", [(0, 0), (1234, 1), (42, 3), (None, None)])
async def test_followup_pr_details(size, files):
    repo = Repository(
        name="example/project",
        do_review_followup=True,
        do_open_prs=False,
        do_merged_prs=False,
    )
    data = await collect_repositories(
        [repo],
        NOW,
        NOW,
        FakeForge(open_prs=[pr(additions=size, deletions=size, changed_files=files)]),
    )
    tex = generate_latex(Spec(repos=[repo]), data, NOW, NOW, [], False)
    assert r"\href{https://example.com/bob}{\mtngusername{{@}{b}{o}{b}}}" in tex
    if size is None:
        assert r"\textcolor{green!50!black}{+" not in tex
        assert r"\ensuremath{\cdot}" not in tex
    else:
        from mtng.generate import human_int

        assert r"\textcolor{green!50!black}{+" + human_int(size) + "}" in tex
        assert r"\textcolor{red!75!black}{\ensuremath{-}" + human_int(size) + "}" in tex
        assert f"{files} {'file' if files == 1 else 'files'}" in tex


@pytest.mark.parametrize("policy", ["any", "all"])
@pytest.mark.parametrize("state", ["APPROVED", "CHANGES_REQUESTED", "COMMENTED"])
def test_completed_review_without_remaining_request_needs_no_assignment(policy, state):
    # Regression for acts-project/acts#6179: a fulfilled request disappears.
    expected = {
        "APPROVED": "Needs maintainer attention (approved)",
        "CHANGES_REQUESTED": "Awaiting author: bob",
        "COMMENTED": "Review comments, no decision",
    }
    groups = review_followup(
        [pr(6179, reviews=[Review(user=ALICE, state=state)])], review_completion=policy
    )
    assert [g["title"] for g in groups] == [expected[state]]


@pytest.mark.parametrize("policy", ["any", "all"])
@pytest.mark.parametrize("state", ["PENDING", "DISMISSED", "COMMENTED"])
def test_author_or_unsubmitted_review_does_not_clear_unassigned_pr(policy, state):
    reviewer = BOB if state == "COMMENTED" else ALICE
    groups = review_followup(
        [pr(reviews=[Review(user=reviewer, state=state)])],
        review_completion=policy,
    )
    assert [g["title"] for g in groups] == ["Needs assignment"]


def test_unavailable_reviews_do_not_clear_unassigned_pr():
    groups = review_followup(
        [pr(reviews_available=False, reviews=[Review(user=ALICE, state="APPROVED")])],
        review_completion="any",
    )
    assert [g["title"] for g in groups] == ["Review status unknown"]


@pytest.mark.parametrize(
    "states,expected",
    [
        (["CHANGES_REQUESTED", "APPROVED"], "Needs maintainer attention (approved)"),
        (["APPROVED", "CHANGES_REQUESTED"], "Awaiting author: bob"),
        (["CHANGES_REQUESTED", "COMMENTED"], "Awaiting author: bob"),
        (["CHANGES_REQUESTED", "PENDING"], "Awaiting author: bob"),
        (["CHANGES_REQUESTED", "DISMISSED"], "Awaiting review: alice"),
        (["APPROVED", "DISMISSED"], "Awaiting review: alice"),
    ],
)
def test_latest_decision_drives_next_action(states, expected):
    reviews = [
        Review(user=ALICE, state=state, submitted_at=NOW + timedelta(hours=i))
        for i, state in enumerate(states)
    ]
    groups = review_followup(
        [pr(assignees=[ALICE], reviews=list(reversed(reviews)))],
        review_completion="any",
    )
    assert [g["title"] for g in groups] == [expected]


def test_requested_changes_override_other_approval_and_pending_requests():
    other = User(login="carol", html_url="https://example.com/carol")
    groups = review_followup(
        [
            pr(
                requested_reviewers=[other],
                reviews=[
                    Review(user=ALICE, state="CHANGES_REQUESTED"),
                    Review(user=other, state="APPROVED"),
                ],
            )
        ],
        review_completion="any",
    )
    assert [g["title"] for g in groups] == ["Awaiting author: bob"]


@pytest.mark.asyncio
async def test_empty_followup_has_no_action_message():
    repo = Repository(
        name="example/project",
        do_review_followup=True,
        do_open_prs=False,
        do_merged_prs=False,
    )
    data = await collect_repositories([repo], NOW, NOW, FakeForge(open_prs=[]))
    assert "No non-draft PRs need follow-up." in generate_latex(
        Spec(repos=[repo]), data, NOW, NOW, [], False
    )


@pytest.mark.asyncio
async def test_followup_only_has_standard_pr_badges():
    repo = Repository(
        name="example/project",
        do_review_followup=True,
        do_open_prs=False,
        do_merged_prs=False,
    )
    item = pr()
    item.title = "feat: Improve report generation"
    data = await collect_repositories(
        [repo], NOW - timedelta(days=7), NOW, FakeForge(open_prs=[item])
    )
    tex = generate_latex(
        Spec(repos=[repo]), data, NOW - timedelta(days=7), NOW, [], False
    )
    assert r"\propen\prnew\prcategory{Feature}" in tex
    assert r"\prstr{1}" in tex
    from mtng.generate import title_units

    assert str(title_units("Improve report generation")) in tex
    assert "Likely next action" not in tex


@pytest.mark.parametrize("state", ["APPROVED", "COMMENTED", "CHANGES_REQUESTED"])
@pytest.mark.parametrize("policy", ["any", "all"])
def test_changed_head_needs_original_reviewer(state, policy):
    groups = review_followup(
        [
            pr(
                head_sha="new",
                reviews=[Review(user=ALICE, state=state, commit_id="old")],
            )
        ],
        review_completion=policy,
    )
    assert [g["title"] for g in groups] == ["Awaiting review: alice"]
    assert groups[0]["actor"] == ALICE
    assert groups[0]["items"] == []
    assert groups[0]["rereview"]["kind"] == "changes_since_review"
    assert [item.number for item in groups[0]["rereview"]["items"]] == [1]


@pytest.mark.parametrize(
    "head,reviewed", [(None, "old"), ("new", None), ("same", "same")]
)
def test_unknown_or_unchanged_head_does_not_imply_changes(head, reviewed):
    groups = review_followup(
        [
            pr(
                head_sha=head,
                reviews=[Review(user=ALICE, state="COMMENTED", commit_id=reviewed)],
            )
        ],
        review_completion="any",
    )
    assert [g["title"] for g in groups] == ["Review comments, no decision"]


def test_new_comment_review_clears_changes_since_review():
    groups = review_followup(
        [
            pr(
                head_sha="new",
                reviews=[
                    Review(
                        user=ALICE, state="COMMENTED", commit_id="new", submitted_at=NOW
                    ),
                    Review(
                        user=ALICE,
                        state="COMMENTED",
                        commit_id="old",
                        submitted_at=NOW - timedelta(days=1),
                    ),
                    Review(
                        user=ALICE,
                        state="PENDING",
                        commit_id="old",
                        submitted_at=NOW + timedelta(days=1),
                    ),
                ],
            )
        ],
        review_completion="any",
    )
    assert [g["title"] for g in groups] == ["Review comments, no decision"]


@pytest.mark.parametrize(
    "policy,expected",
    [
        ("any", "Needs maintainer attention (approved)"),
        ("all", "Awaiting review: alice"),
    ],
)
def test_current_review_satisfies_any_policy(policy, expected):
    carol = User(login="carol", html_url="https://example.com/carol")
    groups = review_followup(
        [
            pr(
                head_sha="new",
                reviews=[
                    Review(user=ALICE, state="COMMENTED", commit_id="old"),
                    Review(user=carol, state="APPROVED", commit_id="new"),
                ],
            )
        ],
        review_completion=policy,
    )
    assert [g["title"] for g in groups] == [expected]


@pytest.mark.parametrize("state", ["PENDING", "DISMISSED"])
def test_unsubmitted_or_dismissed_review_does_not_trigger_changed_head(state):
    groups = review_followup(
        [pr(head_sha="new", reviews=[Review(user=ALICE, state=state, commit_id="old")])]
    )
    assert [g["title"] for g in groups] == ["Needs assignment"]


def test_changed_head_metadata_survives_serialization():
    item = pr(
        head_sha="new", reviews=[Review(user=ALICE, state="COMMENTED", commit_id="old")]
    )
    replayed = PullRequest.model_validate_json(item.model_dump_json())
    group = review_followup([replayed])[0]
    assert group["title"] == "Awaiting review: alice"
    assert group["rereview"]["items"] == [replayed]


def test_changes_since_review_goes_to_latest_reviewer():
    carol = User(login="carol", html_url="https://example.com/carol")
    groups = review_followup(
        [
            # No timestamps: input order decides, so carol reviewed last.
            pr(
                1,
                head_sha="new",
                reviews=[
                    Review(user=user, state="COMMENTED", commit_id="old")
                    for user in [ALICE, carol]
                ],
            ),
            # alice's second review is the latest, even though carol's
            # first-seen review came after alice's first.
            pr(
                2,
                head_sha="new",
                reviews=[
                    Review(
                        user=ALICE,
                        state="COMMENTED",
                        commit_id="old",
                        submitted_at=NOW - timedelta(days=3),
                    ),
                    Review(
                        user=carol,
                        state="COMMENTED",
                        commit_id="old",
                        submitted_at=NOW - timedelta(days=2),
                    ),
                    Review(
                        user=ALICE,
                        state="COMMENTED",
                        commit_id="old",
                        submitted_at=NOW - timedelta(days=1),
                    ),
                ],
            ),
        ],
        review_completion="all",
    )
    assert [
        (g["title"], g["actor"], [item.number for item in g["rereview"]["items"]])
        for g in groups
    ] == [
        ("Awaiting review: alice", ALICE, [2]),
        ("Awaiting review: carol", carol, [1]),
    ]


def test_rereview_joins_reviewers_awaiting_review_group():
    groups = review_followup(
        [
            pr(1, head_sha="new", requested_reviewers=[ALICE]),
            pr(
                2,
                head_sha="new",
                reviews=[Review(user=ALICE, state="COMMENTED", commit_id="old")],
            ),
        ]
    )
    assert len(groups) == 1
    assert groups[0]["title"] == "Awaiting review: alice"
    assert [item.number for item in groups[0]["items"]] == [1]
    assert [item.number for item in groups[0]["rereview"]["items"]] == [2]
    assert "rereview" not in review_followup([pr(1, requested_reviewers=[ALICE])])[0]
