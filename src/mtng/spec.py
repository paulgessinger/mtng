from typing import List, Optional
import pydantic
from pydantic import validator, root_validator


class BaseModel(pydantic.BaseModel):
    class Config:
        extra = "forbid"


class Repository(BaseModel):
    name: str = pydantic.Field(
        ...,
        description="Name of the repository, e.g. 'acts-project/acts'.",
    )
    wip_label: Optional[str] = pydantic.Field(
        None, title="WIP label", description="Label to identify WIP PRs."
    )
    show_wip: bool = pydantic.Field(
        False,
        title="Show WIP PRs",
        description="If true, WIP PRs will be included in the output, else they are ignored.",
    )
    filter_labels: List[str] = pydantic.Field(
        default_factory=list,
        description="If any PR or issue has any label that matches any of these labels, they are excluded.",
    )
    stale_label: Optional[str] = pydantic.Field(
        None,
        description="A label to identify stale PRs/issues. If set, stale PRs and issues will be listed separately and split into newly and other stale items.",
    )

    do_open_prs: bool = pydantic.Field(
        True, title="Do open PRs", description="Show a list of open PRs."
    )
    do_merged_prs: bool = pydantic.Field(
        True, title="Do merged PRs", description="Show a list of merged PRs."
    )
    do_recent_issues: bool = pydantic.Field(
        False,
        title="Do recent issues",
        description="Show a list of issues opened in the time interval.",
    )

    do_assignee: bool = pydantic.Field(
        False,
        title="Do assignee",
        decription="Show assignees",
    )

    no_assignee_attention: bool = pydantic.Field(
        True, description="Draw attention to items without an assignee"
    )

    do_reviewers: bool = pydantic.Field(
        False,
        title="Do reviewers",
        decription="Show reviewers, or requested reviewers",
    )

    needs_discussion_label: Optional[str] = pydantic.Field(
        None,
        title="Label for items to list as 'needs discussion'",
        decription="Adds the item to a dedicated group of slides",
    )

    @property
    def do_stale(self):
        return self.stale_label is not None


class Spec(BaseModel):
    repos: List[Repository]
