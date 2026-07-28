from typing import Dict, List, Optional, Union
import pydantic
from pydantic import ConfigDict


class BaseModel(pydantic.BaseModel):
    model_config = ConfigDict(extra="forbid")


DEFAULT_PR_CATEGORY_COLORS = {
    "feat": "LimeGreen",
    "fix": "Purple",
    "perf": "TealBlue",
    "refactor": "CadetBlue",
    "docs": "CornflowerBlue",
    "test": "RoyalBlue",
    "build": "Goldenrod",
    "ci": "Orange",
    "chore": "Gray",
    "revert": "Mahogany",
    "other": "beige",
    "breaking": "alertred",
}

DEFAULT_PR_CATEGORY_LABELS = {
    "feat": "Feature",
    "fix": "Bugfix",
    "perf": "Performance",
    "refactor": "Refactor",
    "docs": "Docs",
    "test": "Tests",
    "build": "Build",
    "ci": "CI",
    "chore": "Chore",
    "revert": "Revert",
    "other": "Other",
}

# Emoji (CLDR short names, as understood by the `emoji` LaTeX package) shown in
# front of the category name on grouped section pages.
DEFAULT_PR_CATEGORY_EMOJI = {
    "feat": "rocket",
    "fix": "bug",
    "perf": "high-voltage",
    "refactor": "recycling-symbol",
    "docs": "books",
    "test": "test-tube",
    "build": "hammer",
    "ci": "robot",
    "chore": "gear",
    "revert": "counterclockwise-arrows-button",
    "other": "package",
}

# Frame titles, by frame kind. All of them accept the {repo}, {release},
# {category}, {range}, {since} and {date} placeholders; a placeholder that
# expands to nothing takes its separator (or its empty parentheses) with it.
DEFAULT_FRAME_TITLES = {
    "merged_prs": "{repo}: PRs merged {range} ({category})",
    "release_prs": "{repo}: PRs in release {release} ({category})",
    "open_prs": "{repo}: Open PRs ({category})",
    "recent_issues": "{repo}: Issues opened since {since}",
    "new_stale": "{repo}: New stale Issues / PRs since {since}",
    "all_stale": "{repo}: All stale Issues / PRs",
    "needs_discussion": "Needs discussion",
    "stats": "{repo}: period at a glance",
    "release_stats": "{repo}: release {release} at a glance",
}

# Order of the grouped category sections. Categories a repository does not list
# explicitly are appended in this order.
DEFAULT_PR_CATEGORY_ORDER = [
    "feat",
    "fix",
    "refactor",
    "perf",
    "docs",
    "test",
    "build",
    "ci",
    "chore",
    "revert",
    "other",
]


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
    show_pr_categories: bool = pydantic.Field(
        True,
        title="Show PR categories",
        description="If true, parse conventional-commit type prefixes from PR titles and display them as category pills.",
    )
    group_prs_by_category: bool = pydantic.Field(
        False,
        title="Group PRs by category",
        description="If true, open and merged PR slides are grouped into one section per parsed PR category.",
    )
    pr_category_colors: Dict[str, str] = pydantic.Field(
        default_factory=lambda: dict(DEFAULT_PR_CATEGORY_COLORS),
        title="PR category colors",
        description="Map from PR category to LaTeX color name used by category pills. The special key 'breaking' controls the color of breaking-change pills.",
    )
    pr_category_labels: Dict[str, str] = pydantic.Field(
        default_factory=lambda: dict(DEFAULT_PR_CATEGORY_LABELS),
        title="PR category labels",
        description="Map from PR category key to display label shown in pills and grouped section titles (for example feat -> Feature).",
    )
    pr_category_order: List[str] = pydantic.Field(
        default_factory=lambda: list(DEFAULT_PR_CATEGORY_ORDER),
        title="PR category order",
        description="Order of the grouped category sections. Categories that are not listed follow in the default order.",
    )
    frame_titles: Union[str, Dict[str, str]] = pydantic.Field(
        default_factory=lambda: dict(DEFAULT_FRAME_TITLES),
        title="Frame titles",
        description=(
            "Override the title of a kind of frame, either as one string used for every "
            "frame or as a map. Valid keys are "
            + ", ".join(DEFAULT_FRAME_TITLES)
            + ". Supports the {repo}, {release}, {category}, {range}, {since} and {date} placeholders."
        ),
    )

    @pydantic.model_validator(mode="after")
    def check_frame_titles(self) -> "Repository":
        # A bare string is shorthand for "this title, on every frame".
        if isinstance(self.frame_titles, str):
            self.frame_titles = {key: self.frame_titles for key in DEFAULT_FRAME_TITLES}
            return self

        unknown = sorted(set(self.frame_titles) - set(DEFAULT_FRAME_TITLES))
        if unknown:
            raise ValueError(
                f"Unknown frame_titles key(s): {', '.join(unknown)}. "
                f"Valid keys are: {', '.join(DEFAULT_FRAME_TITLES)}."
            )
        return self

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
        description="Deck title shown on the title slide. Supports the {release}, {repos}, {since} and {date} placeholders. If omitted, mtng uses the default repository-based title.",
    )
    footline_left: Optional[str] = pydantic.Field(
        None,
        description="Left-side text shown in the footline. Supports the {release}, {repos}, {since} and {date} placeholders. If omitted, mtng is used.",
    )
    repos: List[Repository]
