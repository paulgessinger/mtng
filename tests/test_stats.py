from datetime import datetime, timedelta

import pytest

from mtng.collect import Label, PullRequest, Review, User
from mtng.stats import compute_release_stats

BASE = datetime(2024, 3, 1, 12, 0, 0)


def make_pr(
    number=1,
    author="someone",
    labels=(),
    reviews=(),
    additions=None,
    deletions=None,
    changed_files=None,
    commits=None,
    merged_at=None,
    closed_at=BASE,
):
    return PullRequest(
        title=f"PR {number}",
        user=User(login=author, html_url="https://example.com"),
        labels=[Label(name=name) for name in labels],
        number=number,
        html_url="https://example.com",
        url="https://example.com",
        reviews=[
            Review(user=User(login=login, html_url="https://example.com"), state=state)
            for login, state in reviews
        ],
        updated_at=BASE,
        created_at=BASE - timedelta(days=1),
        closed_at=closed_at,
        additions=additions,
        deletions=deletions,
        changed_files=changed_files,
        commits=commits,
        merged_at=merged_at,
    )


def test_empty():
    stats = compute_release_stats([])
    assert stats.pr_count == 0
    assert stats.contributors == 0
    assert stats.reviewers == 0
    assert not stats.has_churn
    assert not stats.has_span
    assert stats.top_labels == []


def test_contributors_deduplicated():
    prs = [
        make_pr(1, author="alice"),
        make_pr(2, author="bob"),
        make_pr(3, author="alice"),
    ]
    stats = compute_release_stats(prs)
    assert stats.pr_count == 3
    assert stats.contributors == 2


def test_reviewers_exclude_authors_and_uncounted_states():
    prs = [
        make_pr(
            1,
            author="alice",
            # alice reviewing her own PR must not count.
            reviews=[("alice", "COMMENTED"), ("bob", "APPROVED")],
        ),
        make_pr(
            2,
            author="alice",
            # bob again (dedup), plus states that don't count as a review.
            reviews=[("bob", "CHANGES_REQUESTED"), ("carol", "PENDING")],
        ),
        make_pr(3, author="alice", reviews=[("dave", "DISMISSED")]),
    ]
    stats = compute_release_stats(prs)
    assert stats.reviewers == 1


def test_churn_all_present():
    prs = [
        make_pr(1, additions=10, deletions=3, changed_files=2, commits=4),
        make_pr(2, additions=5, deletions=1, changed_files=1, commits=1),
    ]
    stats = compute_release_stats(prs)
    assert stats.has_churn
    assert stats.churn_complete
    assert (stats.additions, stats.deletions) == (15, 4)
    assert (stats.changed_files, stats.commits) == (3, 5)


def test_churn_all_absent():
    """Mirrors tests/ref/merged_prs.json, which predates the churn fields."""
    stats = compute_release_stats([make_pr(1), make_pr(2)])
    assert not stats.has_churn
    assert stats.churn_complete is False
    assert stats.additions is None
    assert stats.deletions is None
    assert stats.changed_files is None
    assert stats.commits is None


def test_churn_partial_sums_present_values_only():
    prs = [
        make_pr(1, additions=10, deletions=3, changed_files=2, commits=4),
        make_pr(2),
    ]
    stats = compute_release_stats(prs)
    assert stats.has_churn
    assert stats.churn_complete is False
    assert stats.additions == 10


def test_span_same_day_is_one():
    prs = [
        make_pr(1, closed_at=BASE),
        make_pr(2, closed_at=BASE + timedelta(hours=5)),
    ]
    stats = compute_release_stats(prs)
    assert stats.has_span
    assert stats.span_days == 1


def test_span_is_inclusive():
    prs = [
        make_pr(1, closed_at=BASE),
        make_pr(2, closed_at=BASE + timedelta(days=3)),
    ]
    stats = compute_release_stats(prs)
    assert stats.span_days == 4
    assert stats.first_merged_at == BASE
    assert stats.last_merged_at == BASE + timedelta(days=3)


def test_merged_at_wins_over_closed_at():
    merged = BASE + timedelta(days=10)
    stats = compute_release_stats([make_pr(1, merged_at=merged, closed_at=BASE)])
    assert stats.first_merged_at == merged
    assert stats.last_merged_at == merged


def test_span_falls_back_to_closed_at():
    stats = compute_release_stats([make_pr(1, merged_at=None, closed_at=BASE)])
    assert stats.first_merged_at == BASE


def test_no_timestamps_leaves_span_unset():
    stats = compute_release_stats([make_pr(1, merged_at=None, closed_at=None)])
    assert not stats.has_span
    assert stats.span_days is None


def test_labels_ranked_and_truncated():
    prs = [
        make_pr(1, labels=["bug", "feature"]),
        make_pr(2, labels=["bug", "docs"]),
        make_pr(3, labels=["bug", "ci", "infra"]),
    ]
    stats = compute_release_stats(prs, top_labels=2)
    assert [(l.name, l.count) for l in stats.top_labels] == [("bug", 3), ("ci", 1)]


def test_label_ties_broken_alphabetically():
    prs = [make_pr(1, labels=["zebra", "apple", "mango"])]
    stats = compute_release_stats(prs)
    assert [l.name for l in stats.top_labels] == ["apple", "mango", "zebra"]


def test_ignore_labels_filtered_out():
    prs = [
        make_pr(1, labels=["bug", "WIP"]),
        make_pr(2, labels=["bug", "Stale"]),
    ]
    stats = compute_release_stats(prs, ignore_labels=["WIP", "Stale"])
    assert [l.name for l in stats.top_labels] == ["bug"]
