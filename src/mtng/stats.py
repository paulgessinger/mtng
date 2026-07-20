from collections import Counter
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Sequence, Tuple

import pydantic

if TYPE_CHECKING:  # pragma: no cover - avoids a circular import with collect.py
    from mtng.collect import PullRequest


# Review states that indicate someone actually looked at the PR. PENDING reviews
# were never submitted, and DISMISSED ones no longer count towards the PR.
COUNTED_REVIEW_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "COMMENTED"})


class LabelCount(pydantic.BaseModel):
    name: str
    count: int


class ReleaseStats(pydantic.BaseModel):
    pr_count: int = 0
    contributors: int = 0
    reviewers: int = 0

    additions: Optional[int] = None
    deletions: Optional[int] = None
    changed_files: Optional[int] = None
    commits: Optional[int] = None

    # False when at least one PR was missing churn data, i.e. the sums above are
    # lower bounds rather than exact totals.
    churn_complete: bool = True

    first_merged_at: Optional[datetime] = None
    last_merged_at: Optional[datetime] = None
    span_days: Optional[int] = None

    top_labels: List[LabelCount] = pydantic.Field(default_factory=list)

    @property
    def has_churn(self) -> bool:
        return any(
            v is not None
            for v in (self.additions, self.deletions, self.changed_files, self.commits)
        )

    @property
    def has_span(self) -> bool:
        return self.first_merged_at is not None and self.last_merged_at is not None


def _sum_optional(values: Sequence[Optional[int]]) -> Tuple[Optional[int], bool]:
    """Sum ignoring None. Returns (total or None if nothing present, complete)."""
    present = [v for v in values if v is not None]
    total = sum(present) if len(present) > 0 else None
    return total, len(present) == len(values)


def compute_release_stats(
    prs: Sequence["PullRequest"],
    *,
    top_labels: int = 4,
    ignore_labels: Sequence[str] = (),
) -> ReleaseStats:
    """Aggregate the headline numbers for a set of PRs that landed in a release."""

    if len(prs) == 0:
        return ReleaseStats()

    authors = {pr.user.login for pr in prs}

    # Exclude authors so that self-comments don't inflate the reviewer count.
    reviewers = {
        review.user.login
        for pr in prs
        for review in pr.reviews
        if review.state in COUNTED_REVIEW_STATES
    } - authors

    additions, additions_ok = _sum_optional([pr.additions for pr in prs])
    deletions, deletions_ok = _sum_optional([pr.deletions for pr in prs])
    changed_files, changed_files_ok = _sum_optional([pr.changed_files for pr in prs])
    commits, commits_ok = _sum_optional([pr.commits for pr in prs])

    # merged_at is only available from the single-PR endpoint; closed_at is the
    # next best thing and is always present on a merged PR.
    merge_times = [
        t for t in (pr.merged_at or pr.closed_at for pr in prs) if t is not None
    ]
    first_merged_at = min(merge_times) if len(merge_times) > 0 else None
    last_merged_at = max(merge_times) if len(merge_times) > 0 else None
    span_days = None
    if first_merged_at is not None and last_merged_at is not None:
        # Inclusive, so a release developed in a single day reads "1 day".
        span_days = (last_merged_at.date() - first_merged_at.date()).days + 1

    ignored = set(ignore_labels)
    counts = Counter(
        label.name for pr in prs for label in pr.labels if label.name not in ignored
    )
    # Sort explicitly rather than relying on most_common's insertion-order
    # tiebreak, so equal counts are ordered deterministically.
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:top_labels]

    return ReleaseStats(
        pr_count=len(prs),
        contributors=len(authors),
        reviewers=len(reviewers),
        additions=additions,
        deletions=deletions,
        changed_files=changed_files,
        commits=commits,
        churn_complete=additions_ok
        and deletions_ok
        and changed_files_ok
        and commits_ok,
        first_merged_at=first_merged_at,
        last_merged_at=last_merged_at,
        span_days=span_days,
        top_labels=[LabelCount(name=name, count=count) for name, count in ranked],
    )
