from typing import List, Optional
import pydantic
from pydantic import ConfigDict


class BaseModel(pydantic.BaseModel):
    model_config = ConfigDict(extra="forbid")


class Repository(BaseModel):
    name: str = pydantic.Field(
        ...,
        description="Name of the repository, e.g. 'acts-project/acts'.",
    )
    display_name: Optional[str] = pydantic.Field(
        None,
        title="Display name",
        description="Alternative repository name. Useful when fetching one repository multiple times.",
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
        description="If any PR or issue has any label that matches any of these labels, they are excluded. Mutually exclusive with 'include_labels' and 'exclude_labels'.",
    )
    include_labels: List[str] = pydantic.Field(
        default_factory=list,
        description="If set, only PRs or issues that have all of these labels are included. Mutually exclusive with 'filter_labels'.",
    )
    exclude_labels: List[str] = pydantic.Field(
        default_factory=list,
        description="If any PR or issue has any label that matches any of these labels, they are excluded. Mutually exclusive with 'filter_labels'.",
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
        description="Show assignees",
    )

    no_assignee_attention: bool = pydantic.Field(
        True, description="Draw attention to items without an assignee"
    )

    do_reviewers: bool = pydantic.Field(
        False,
        title="Do reviewers",
        description="Show reviewers, or requested reviewers",
    )
    show_review_summary: bool = pydantic.Field(
        True,
        title="Show review summary",
        description="Show review outcome text such as 'reviewed by', comments, and requested changes.",
    )

    needs_discussion_label: Optional[str] = pydantic.Field(
        None,
        title="Label for items to list as 'needs discussion'",
        description="Adds the item to a dedicated group of slides",
    )

    @pydantic.model_validator(mode="after")
    def check_label_filters(self) -> "Repository":
        if self.filter_labels and (self.include_labels or self.exclude_labels):
            raise ValueError(
                "'filter_labels' is mutually exclusive with 'include_labels' and 'exclude_labels'."
            )
        return self

    @property
    def with_labels(self) -> List[str]:
        return self.include_labels

    @property
    def without_labels(self) -> List[str]:
        return self.filter_labels + self.exclude_labels

    @property
    def do_stale(self):
        return self.stale_label is not None


class Spec(BaseModel):
    title: Optional[str] = pydantic.Field(
        None,
        description="Deck title shown on the title slide. If omitted, mtng uses the default repository-based title.",
    )
    footline_left: Optional[str] = pydantic.Field(
        None,
        description="Left-side text shown in the footline. If omitted, mtng is used.",
    )
    repos: List[Repository]
