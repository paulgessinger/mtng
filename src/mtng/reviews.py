"""Current assignment follow-up, independent of a PR's approval status."""

from typing import Literal

from mtng.models import PullRequest
from mtng.stats import COUNTED_REVIEW_STATES


def review_followup(
    prs: list[PullRequest],
    wip_label: str | None = None,
    assignment_source: Literal["assignees", "requested_reviewers", "both"] = "both",
) -> list[dict]:
    unassigned = []
    outstanding = {}
    unknown = {}
    for pr in sorted(prs, key=lambda pr: (pr.updated_at, pr.created_at, pr.number)):
        if pr.draft or (wip_label and any(l.name == wip_label for l in pr.labels)):
            continue
        assignees = pr.assignees or ([pr.assignee] if pr.assignee else [])
        reviewers = {}
        people = []
        if assignment_source in {"assignees", "both"}:
            people.extend(assignees)
        if assignment_source in {"requested_reviewers", "both"}:
            people.extend(pr.requested_reviewers)
        for user in people:
            reviewers.setdefault(user.login.casefold(), user.login)
        if not reviewers:
            unassigned.append(pr)
            continue
        reviewed = (
            {
                review.user.login.casefold()
                for review in pr.reviews
                if review.state in COUNTED_REVIEW_STATES
            }
            if pr.reviews_available
            else set()
        )
        for login in sorted(reviewers.values(), key=str.casefold):
            if login.casefold() in reviewed:
                continue
            target = (
                outstanding
                if pr.reviews_available and pr.review_activity_available
                else unknown
            )
            target.setdefault(login, []).append(pr)
    groups = []
    if unassigned:
        groups.append(dict(title="Needs assignment", items=unassigned))
    for title, people in [
        ("Awaiting review", outstanding),
        ("Review status unknown", unknown),
    ]:
        for login, items in sorted(people.items(), key=lambda pair: pair[0].casefold()):
            groups.append(dict(title=f"{title}: {login}", items=items))
    return groups
