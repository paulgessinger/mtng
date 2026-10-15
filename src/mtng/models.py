"""Forge-neutral data models.

Every forge backend translates its API responses into these, and the templates
and stats only ever read them, so they are the contract between the two. Field
names follow the GitHub REST payloads, which is what these were modelled on.
"""

from typing import Any, Literal
from datetime import datetime

import pydantic


class Label(pydantic.BaseModel):
    name: str


class User(pydantic.BaseModel):
    login: str
    html_url: str


class Review(pydantic.BaseModel):
    user: User
    state: Literal["APPROVED", "COMMENTED", "CHANGES_REQUESTED", "DISMISSED", "PENDING"]
    body: str | None = None

    submitted_at: datetime | None = None


class IssueBase(pydantic.BaseModel):
    title: str
    user: User
    labels: list[Label]
    html_url: str
    number: int
    assignee: User | None = None

    body: str | None = None
    url: str

    updated_at: datetime
    created_at: datetime
    closed_at: datetime | None = None

    is_wip: bool = False
    is_stale: bool = False
    llm_summary: str | None = None
    pr_title_display: str | None = None
    pr_category: str | None = None
    pr_category_label: str | None = None
    pr_category_breaking: bool = False
    pr_category_color: str | None = None
    pr_category_order: int = 999

    draft: bool | None = None


class Issue(IssueBase):
    pull_request: Any | None = None

    @property
    def is_pr(self) -> bool:
        return self.pull_request is not None


class PullRequest(IssueBase):
    reviews_available: bool = True
    requested_reviewers: list[User] = pydantic.Field(default_factory=list)
    reviews: list[Review] = pydantic.Field(default_factory=list)

    # Only populated by GET /repos/{repo}/pulls/{n}. The /search/issues endpoint
    # never returns these, so anything sourced from a search stays None.
    additions: int | None = None
    deletions: int | None = None
    changed_files: int | None = None
    commits: int | None = None
    merged_at: datetime | None = None

    @property
    def is_pr(self) -> bool:
        return True
