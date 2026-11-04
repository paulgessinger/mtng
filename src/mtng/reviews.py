"""Infer likely next actions from current assignments and submitted reviews."""

from typing import Literal

from mtng.models import PullRequest
from mtng.stats import COUNTED_REVIEW_STATES

# Canonical identifiers for review-follow-up group kinds, stamped onto every
# group as ``kind``. Section specs select summaries with these names, so this
# list is configuration API: additions are safe, renames are breaking.
FOLLOWUP_GROUP_KINDS = (
    "needs_assignment",
    "awaiting_author",
    "awaiting_review",
    "changes_since_review",
    "needs_clarification",
    "needs_maintainer_attention",
    "review_status_unknown",
)


def _group_kind(title: str) -> str:
    """Canonical kind for a group title, tolerating actor suffixes and
    parenthetical details ("Review status unknown: bob", "Needs maintainer
    attention (approved)")."""

    return _GROUP_KINDS[title.rsplit(":", 1)[0].split(" (", 1)[0].strip()]


_GROUP_KINDS = {
    "Needs assignment": "needs_assignment",
    "Awaiting author": "awaiting_author",
    "Awaiting review": "awaiting_review",
    "Changes since review": "changes_since_review",
    "Needs clarification": "needs_clarification",
    "Needs maintainer attention": "needs_maintainer_attention",
    "Review status unknown": "review_status_unknown",
}


def review_followup(
    prs: list[PullRequest],
    wip_label: str | None = None,
    assignment_source: Literal["assignees", "requested_reviewers", "both"] = "both",
    review_completion: Literal["any", "all"] = "all",
) -> list[dict]:
    groups: dict[str, list[PullRequest]] = {}

    actors = {}

    def add(title, pr, actor=None):
        groups.setdefault(title, []).append(pr)
        if actor is not None:
            actors.setdefault(title, actor)

    for pr in sorted(prs, key=lambda pr: (pr.updated_at, pr.created_at, pr.number)):
        if pr.draft or (wip_label and any(l.name == wip_label for l in pr.labels)):
            continue
        assignees = pr.assignees or ([pr.assignee] if pr.assignee else [])
        people = []
        if assignment_source in {"assignees", "both"}:
            people.extend(assignees)
        if assignment_source in {"requested_reviewers", "both"}:
            people.extend(pr.requested_reviewers)
        reviewers = {user.login.casefold(): user.login for user in people}
        reviewer_users = {user.login.casefold(): user for user in people}
        reviewed = set()
        decisions = {}
        latest_reviews = {}
        if pr.reviews_available:
            for _, review in sorted(
                enumerate(pr.reviews),
                key=lambda pair: (
                    (
                        pair[1].submitted_at.timestamp()
                        if pair[1].submitted_at
                        else float("-inf")
                    ),
                    pair[0],
                ),
            ):
                login = review.user.login.casefold()
                if login == pr.user.login.casefold():
                    continue
                if review.state in COUNTED_REVIEW_STATES:
                    reviewed.add(login)
                    latest_reviews[login] = review
                # A later comment or pending review doesn't revoke a decision.
                # A dismissal does; don't resurrect an earlier approval.
                if review.state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
                    decisions[login] = review.state
                if review.state == "DISMISSED":
                    reviewed.discard(login)
                    latest_reviews.pop(login, None)

        outdated = {
            login: review
            for login, review in latest_reviews.items()
            if pr.head_sha and review.commit_id and review.commit_id != pr.head_sha
        }
        changes_requested = {
            login for login, state in decisions.items() if state == "CHANGES_REQUESTED"
        }
        if changes_requested - outdated.keys():
            add(f"Awaiting author: {pr.user.login}", pr, pr.user)
            continue

        # With "any", a review of the current head satisfies participation;
        # an outstanding changes request still needs its reviewer's attention.
        current_reviews = {
            login
            for login, review in latest_reviews.items()
            if pr.head_sha and review.commit_id == pr.head_sha
        }
        if review_completion == "any" and current_reviews:
            outdated = {
                login: review
                for login, review in outdated.items()
                if login in changes_requested
            }
        if outdated and pr.review_activity_available:
            add("Changes since review", pr)
            if review_completion == "all":
                for login in sorted(reviewers.keys() - reviewed):
                    add(
                        f"Awaiting review: {reviewers[login]}",
                        pr,
                        reviewer_users[login],
                    )
            continue

        complete = bool(reviewed) and (
            review_completion == "any" or not (reviewers.keys() - reviewed)
        )
        if not pr.reviews_available or not pr.review_activity_available:
            # Partial approval-only data cannot rule out requested changes.
            pending = reviewers.keys() - reviewed
            for login in sorted(pending):
                add(
                    f"Review status unknown: {reviewers[login]}",
                    pr,
                    reviewer_users[login],
                )
            if not pending:
                add("Review status unknown", pr)
        elif complete:
            if "APPROVED" in decisions.values():
                add("Needs maintainer attention (approved)", pr)
            else:
                add("Needs clarification (review comments)", pr)
        elif reviewers:
            for login in sorted(reviewers.keys() - reviewed):
                add(f"Awaiting review: {reviewers[login]}", pr, reviewer_users[login])
        else:
            add("Needs assignment", pr)

    order = [
        "Needs assignment",
        "Awaiting author",
        "Awaiting review",
        "Changes since review",
        "Needs clarification",
        "Needs maintainer attention",
        "Review status unknown",
    ]
    return [
        dict(
            title=title,
            kind=_group_kind(title),
            items=items,
            actor=actors.get(title),
            heading=title.rsplit(":", 1)[0] if title in actors else title,
        )
        for title, items in sorted(
            groups.items(),
            key=lambda pair: (
                next(i for i, prefix in enumerate(order) if pair[0].startswith(prefix)),
                pair[0].casefold(),
            ),
        )
    ]
