import pydantic
from typing import Annotated, Literal
from urllib.parse import urlsplit
import re
from pydantic import ConfigDict


class BaseModel(pydantic.BaseModel):
    model_config = ConfigDict(extra="forbid")


def normalize_host(host: str) -> str:
    """Accept an HTTPS origin or hostname, returning a canonical authority."""
    value = host.strip().lower()
    parsed = urlsplit(value if "://" in value else f"https://{value}")
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"[a-z0-9.-]+(?::[0-9]+)?", parsed.netloc)
    ):
        raise ValueError(
            "host must be an HTTPS hostname, without a path or credentials"
        )
    port = parsed.port  # Also validate the port range.
    return parsed.hostname + (f":{port}" if port and port != 443 else "")


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
    "breaking_changes": "{repo}: Breaking changes",
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
    forge: Literal["github", "gitlab"] = pydantic.Field(
        "github", description="Code hosting backend. Defaults to GitHub."
    )
    host: str | None = pydantic.Field(
        None, description="HTTPS host; defaults to github.com or gitlab.com."
    )
    name: str = pydantic.Field(
        ...,
        description="Name of the repository, e.g. 'acts-project/acts'.",
    )
    display_name: str | None = pydantic.Field(
        None,
        title="Display name",
        description="Alternative repository name. Useful when fetching one repository multiple times.",
    )
    wip_label: str | None = pydantic.Field(
        None, title="WIP label", description="Label to identify WIP PRs."
    )
    show_wip: bool = pydantic.Field(
        False,
        title="Show WIP PRs",
        description="If true, WIP PRs will be included in the output, else they are ignored.",
    )
    filter_labels: list[str] = pydantic.Field(
        default_factory=list,
        description="If any PR or issue has any label that matches any of these labels, they are excluded. Mutually exclusive with 'include_labels' and 'exclude_labels'.",
    )
    include_labels: list[str] = pydantic.Field(
        default_factory=list,
        description="If set, only PRs or issues that have all of these labels are included. Mutually exclusive with 'filter_labels'.",
    )
    exclude_labels: list[str] = pydantic.Field(
        default_factory=list,
        description="If any PR or issue has any label that matches any of these labels, they are excluded. Mutually exclusive with 'filter_labels'.",
    )
    stale_label: str | None = pydantic.Field(
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
        description="Show a compact, colored review status without reviewer usernames.",
    )

    needs_discussion_label: str | None = pydantic.Field(
        None,
        title="Label for items to list as 'needs discussion'",
        description="Adds the item to a dedicated group of slides",
    )
    show_pr_categories: bool = pydantic.Field(
        True,
        title="Show PR categories",
        description="If true, parse conventional-commit type prefixes from PR titles and display them as category pills.",
    )
    truncate_pr_titles: bool = pydantic.Field(
        False,
        description="Shorten PR titles in the middle to fit one line, preserving badges and the PR number.",
    )
    group_prs_by_category: bool = pydantic.Field(
        False,
        title="Group PRs by category",
        description="If true, open and merged PR slides are grouped into one section per parsed PR category.",
    )
    pr_category_colors: dict[str, str] = pydantic.Field(
        default_factory=lambda: dict(DEFAULT_PR_CATEGORY_COLORS),
        title="PR category colors",
        description="Map from PR category to LaTeX color name used by category pills. The special key 'breaking' controls the text fallback color of the separate breaking-change marker.",
    )
    pr_category_labels: dict[str, str] = pydantic.Field(
        default_factory=lambda: dict(DEFAULT_PR_CATEGORY_LABELS),
        title="PR category labels",
        description="Map from PR category key to display label shown in pills and grouped section titles (for example feat -> Feature).",
    )
    show_breaking_changes: bool = pydantic.Field(
        True,
        title="Show breaking changes",
        description="If true, merged PRs marked breaking (a '!' in the conventional-commit prefix) also get their own leading section. The section is skipped when there are none.",
    )
    repeat_breaking_changes: bool = pydantic.Field(
        True,
        title="Repeat breaking changes",
        description="If true, breaking PRs stay in the regular merged/category slides in addition to the breaking changes section. If false, they are listed there only.",
    )
    pr_category_order: list[str] = pydantic.Field(
        default_factory=lambda: list(DEFAULT_PR_CATEGORY_ORDER),
        title="PR category order",
        description="Order of the grouped category sections. Categories that are not listed follow in the default order.",
    )
    frame_titles: str | dict[str, str] = pydantic.Field(
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
    def check_forge(self) -> "Repository":
        if self.host is not None:
            self.host = normalize_host(self.host)
        if self.forge == "github" and self.resolved_host != "github.com":
            raise ValueError("GitHub Enterprise hosts are not supported yet")
        parts = self.name.split("/")
        if len(parts) < 2 or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]+", part) or part in (".", "..")
            for part in parts
        ):
            raise ValueError(
                "name must be a project path such as group/subgroup/project"
            )
        if self.forge == "github" and len(parts) != 2:
            raise ValueError("GitHub repository names must use owner/repo")
        return self

    @property
    def resolved_host(self) -> str:
        return self.host or f"{self.forge}.com"

    @property
    def forge_key(self) -> tuple[str, str]:
        return self.forge, self.resolved_host

    @pydantic.model_validator(mode="after")
    def check_frame_titles(self) -> "Repository":
        if self.forge == "gitlab" and "frame_titles" not in self.model_fields_set:
            self.frame_titles = {
                key: value.replace("PRs", "MRs")
                for key, value in DEFAULT_FRAME_TITLES.items()
            }
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
    def with_labels(self) -> list[str]:
        return self.include_labels

    @property
    def without_labels(self) -> list[str]:
        return self.filter_labels + self.exclude_labels

    @property
    def do_stale(self):
        return self.stale_label is not None


class LabelFilter(BaseModel):
    include: list[str] = pydantic.Field(default_factory=list)
    exclude: list[str] = pydantic.Field(default_factory=list)


class RepositorySection(BaseModel):
    repositories: list[str] = pydantic.Field(min_length=1)
    title: str | None = None
    labels: LabelFilter = pydantic.Field(default_factory=LabelFilter)

    @pydantic.field_validator("repositories")
    @classmethod
    def unique_repositories(cls, value):
        if len(value) != len(set(value)):
            raise ValueError("A section cannot list the same repository twice")
        return value


class ChangesSection(RepositorySection):
    type: Literal["changes"] = "changes"
    state: Literal["merged", "open"] = "merged"
    release: str | None = None
    group_by: Literal["none", "category"] = "none"
    drafts: Literal["include", "exclude", "only"] | None = None
    summary: bool = True

    @pydantic.model_validator(mode="after")
    def check_release(self):
        if self.release is not None and self.state != "merged":
            raise ValueError("release is only supported for merged changes")
        if self.state != "open" and self.drafts is not None:
            raise ValueError("drafts is only supported for open changes")
        return self


class StatisticsSection(RepositorySection):
    type: Literal["statistics"] = "statistics"
    release: str | None = None


class IssuesSection(RepositorySection):
    type: Literal["issues"] = "issues"


class StaleSection(RepositorySection):
    type: Literal["stale"] = "stale"
    label: str = pydantic.Field(min_length=1)


class DiscussionSection(RepositorySection):
    type: Literal["discussion"] = "discussion"
    label: str = pydantic.Field(min_length=1)


class IndicoSection(BaseModel):
    type: Literal["indico"] = "indico"
    event: str
    title: str = "Contributions today"

    @pydantic.field_validator("event")
    @classmethod
    def check_event(cls, value):
        if not re.fullmatch(r"https://[^/?#]+/event/\d+/?", value):
            raise ValueError("event must be an HTTPS Indico event URL")
        return value


Section = Annotated[
    ChangesSection
    | StatisticsSection
    | IssuesSection
    | StaleSection
    | DiscussionSection
    | IndicoSection,
    pydantic.Field(discriminator="type"),
]


class Spec(BaseModel):
    cache_max_age: int = pydantic.Field(
        300,
        ge=0,
        description="Seconds to reuse cached API responses locally; zero revalidates every time.",
    )
    title: str | None = pydantic.Field(
        None,
        description="Deck title shown on the title slide. Supports the {release}, {repos}, {since} and {date} placeholders. If omitted, mtng uses the default repository-based title.",
    )
    footline_left: str | None = pydantic.Field(
        None,
        description="Left-side text shown in the footline. Supports the {release}, {repos}, {since} and {date} placeholders. If omitted, mtng is used.",
    )
    repos: list[Repository] = pydantic.Field(default_factory=list)
    repositories: dict[str, Repository] = pydantic.Field(default_factory=dict)
    sections: list[Section] | None = None

    @pydantic.model_validator(mode="after")
    def check_sections(self):
        if self.sections is None:
            if self.repositories:
                raise ValueError("repositories requires an explicit sections list")
            return self
        if self.repos:
            raise ValueError(
                "Use either legacy repos or repositories/sections, not both"
            )
        for section in self.sections:
            if isinstance(section, IndicoSection):
                continue
            unknown = set(section.repositories) - self.repositories.keys()
            if unknown:
                raise ValueError(
                    f"Unknown repository aliases: {', '.join(sorted(unknown))}"
                )
        names = [repo.display_name or repo.name for repo in self.source_repositories]
        if len(names) != len(set(names)):
            raise ValueError(
                "Set unique display_name values for distinct repository aliases"
            )
        return self

    @property
    def source_repositories(self) -> list[Repository]:
        if self.sections is None:
            return self.repos
        aliases = dict.fromkeys(
            alias
            for section in self.sections
            if not isinstance(section, IndicoSection)
            for alias in section.repositories
        )
        return [self.repositories[alias] for alias in aliases]

    def needs_since(self, release: str | None) -> bool:
        if self.sections is None:
            return release is None
        return any(
            isinstance(section, IssuesSection | StaleSection)
            or (
                isinstance(section, StatisticsSection | ChangesSection)
                and (
                    not isinstance(section, ChangesSection) or section.state == "merged"
                )
                and section.release is None
                and release is None
            )
            for section in self.sections
        )

    @pydantic.model_validator(mode="after")
    def check_display_keys(self) -> "Spec":
        keys = [repo.display_name or repo.name for repo in self.repos]
        if len(keys) != len(set(keys)):
            raise ValueError(
                "Duplicate repository display names; set unique display_name values"
            )
        return self
