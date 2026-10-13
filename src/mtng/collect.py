from typing import Any
from datetime import datetime
import re

from rich import print
from rich.rule import Rule
from rich.status import Status

from mtng.forge import Forge
from mtng.models import IssueBase
from mtng.spec import (
    DEFAULT_PR_CATEGORY_COLORS,
    DEFAULT_PR_CATEGORY_EMOJI,
    DEFAULT_PR_CATEGORY_LABELS,
    DEFAULT_PR_CATEGORY_ORDER,
    Repository,
)
from mtng.stats import compute_release_stats


CONVENTIONAL_TITLE_RE = re.compile(
    r"^(?P<kind>[a-z][a-z0-9-]*)(?:\([^)]+\))?(?P<breaking>!)?:\s*(?P<subject>.+)$",
    re.IGNORECASE,
)
CATEGORY_ORDER = tuple(DEFAULT_PR_CATEGORY_ORDER)
CATEGORY_ORDER_INDEX = {name: index for index, name in enumerate(CATEGORY_ORDER)}


def parse_pr_metadata(title: str) -> tuple[str, bool, str, bool]:
    m = CONVENTIONAL_TITLE_RE.match(title.strip())
    if m is None:
        return "other", False, title.strip(), False
    return (
        m.group("kind").lower(),
        bool(m.group("breaking")),
        m.group("subject").strip(),
        True,
    )


def parse_pr_category(title: str) -> tuple[str, bool]:
    category, is_breaking, _, _ = parse_pr_metadata(title)
    return category, is_breaking


def resolve_category_order(order: list[str] | None = None) -> dict[str, int]:
    """Position of every known category: the configured keys first, then any
    default category the configuration left out."""
    if order is None:
        return dict(CATEGORY_ORDER_INDEX)
    resolved = list(order)
    resolved += [name for name in DEFAULT_PR_CATEGORY_ORDER if name not in resolved]
    return {name: index for index, name in enumerate(resolved)}


def category_order(category: str, order: list[str] | None = None) -> int:
    index = resolve_category_order(order)
    return index.get(category, len(index))


def resolve_category_color(
    category: str, is_breaking: bool, category_colors: dict[str, str]
) -> str:
    if is_breaking:
        return category_colors.get(
            "breaking", DEFAULT_PR_CATEGORY_COLORS.get("breaking", "alertred")
        )
    return category_colors.get(
        category,
        DEFAULT_PR_CATEGORY_COLORS.get(
            category,
            category_colors.get(
                "other", DEFAULT_PR_CATEGORY_COLORS.get("other", "beige")
            ),
        ),
    )


def resolve_category_label(category: str, category_labels: dict[str, str]) -> str:
    return category_labels.get(
        category,
        DEFAULT_PR_CATEGORY_LABELS.get(
            category,
            category_labels.get(
                "other",
                DEFAULT_PR_CATEGORY_LABELS.get(
                    "other", category.replace("-", " ").strip().capitalize()
                ),
            ),
        ),
    )


def enrich_item(item: IssueBase, repo: Repository) -> None:
    labels = [l.name for l in item.labels]
    item.is_wip = repo.wip_label in labels
    if item.is_pr:
        item.is_wip = item.is_wip or (item.draft if item.draft is not None else False)
    item.is_stale = repo.stale_label in labels

    if not item.is_pr:
        return

    category, is_breaking, display_title, parsed = parse_pr_metadata(item.title)
    item.pr_category = category
    item.pr_category_breaking = is_breaking
    item.pr_category_label = resolve_category_label(category, repo.pr_category_labels)
    item.pr_category_color = resolve_category_color(
        category, is_breaking, repo.pr_category_colors
    )
    item.pr_category_order = category_order(category, repo.pr_category_order)
    if parsed:
        item.pr_title_display = display_title


def group_prs_by_category(items: list[IssueBase]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for item in items:
        if not item.is_pr:
            continue
        category = item.pr_category or "other"
        group = grouped.setdefault(
            category,
            {
                "category": category,
                "title": item.pr_category_label or category,
                "emoji": DEFAULT_PR_CATEGORY_EMOJI.get(category),
                # Set per repository by enrich_item, so the configured
                # pr_category_order carries over to the section sequence.
                "order": item.pr_category_order,
                "items": [],
            },
        )
        group["items"].append(item)
    return sorted(grouped.values(), key=lambda group: (group["order"], group["title"]))


async def collect_repositories(
    repos: list[Repository],
    since: datetime,
    now: datetime,
    forge: Forge,
    release: str | None = None,
):
    data = {}

    for repo in repos:
        print(Rule(f"Collecting data for {repo.name}"))
        key = repo.display_name or repo.name
        data[key] = {}
        data[key]["merged_prs"] = []
        data[key]["merged_prs_listed"] = []
        data[key]["breaking_prs"] = []
        data[key]["open_prs"] = []
        data[key]["stale"] = []
        data[key]["recent_issues"] = []
        data[key]["needs_discussion"] = []
        data[key]["release_tag"] = None
        data[key]["release_stats"] = None
        data[key]["merged_prs_by_category"] = []
        data[key]["open_prs_by_category"] = []
        data[key]["spec"] = repo

        if repo.do_merged_prs:
            print(Rule("Fetching merged PRs", align="left"))
            if release is not None:
                release_tag, merged_prs = await forge.release_prs(
                    repo.name,
                    release,
                    with_labels=repo.with_labels,
                    without_labels=repo.without_labels,
                )
                data[key]["release_tag"] = release_tag
            else:
                merged_prs = await forge.merged_prs(
                    repo.name,
                    since,
                    now,
                    with_labels=repo.with_labels,
                    without_labels=repo.without_labels,
                )
            data[key]["merged_prs"] = merged_prs
            data[key]["release_stats"] = compute_release_stats(
                data[key]["merged_prs"],
                ignore_labels=[
                    label for label in (repo.wip_label, repo.stale_label) if label
                ],
            )
            for pr in data[key]["merged_prs"]:
                enrich_item(pr, repo)

            data[key]["breaking_prs"] = [
                pr for pr in merged_prs if pr.pr_category_breaking
            ]
            # Without repeat_breaking_changes, a breaking PR is only listed on
            # its own slide, not again under its category.
            listed = (
                merged_prs
                if repo.repeat_breaking_changes or not repo.show_breaking_changes
                else [pr for pr in merged_prs if not pr.pr_category_breaking]
            )
            data[key]["merged_prs_listed"] = listed
            data[key]["merged_prs_by_category"] = group_prs_by_category(listed)

        if release is not None:
            # In release mode we intentionally only show PRs that landed in the
            # selected release and skip open/stale/recent/discussion lists.
            continue

        if repo.do_open_prs:
            print(Rule("Fetching open PRs", align="left"))
            open_prs = await forge.open_prs(
                repo.name,
                with_labels=repo.with_labels,
                without_labels=repo.without_labels,
            )

            if not repo.show_wip:
                open_prs = list(
                    filter(
                        lambda pr: repo.wip_label not in [l.name for l in pr.labels],
                        open_prs,
                    )
                )
            data[key]["open_prs"] = open_prs

        if repo.do_stale:
            if repo.stale_label is None:
                raise ValueError("Provide stale label if do_stale=True")
            with Status("Getting stale issues"):
                stale = await forge.open_issues(
                    repo.name,
                    with_labels=[repo.stale_label] + repo.with_labels,
                    without_labels=repo.without_labels,
                    include_prs=True,
                )

                data[key]["stale"] = stale

        if repo.do_recent_issues:
            with Status("Getting recent issues"):
                recent_issues = await forge.open_issues(
                    repo.name,
                    start=since,
                    end=now,
                    with_labels=repo.with_labels,
                    without_labels=repo.without_labels,
                )

                data[key]["recent_issues"] = recent_issues

        for item_type in "open_prs", "stale", "recent_issues":
            for item in data[key][item_type]:
                enrich_item(item, repo)
        data[key]["open_prs_by_category"] = group_prs_by_category(data[key]["open_prs"])

        if repo.needs_discussion_label is not None:
            with Status("Getting items that need discussion"):
                needs_discussion = await forge.open_issues(
                    repo.name,
                    with_labels=[repo.needs_discussion_label] + repo.with_labels,
                    without_labels=repo.without_labels,
                )
                data[key]["needs_discussion"] = needs_discussion
                for item in data[key]["needs_discussion"]:
                    enrich_item(item, repo)

    return data
