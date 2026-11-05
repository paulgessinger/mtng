"""Prepare report content once, independently of output syntax and pagination."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from mtng.models import IssueBase
from mtng.spec import DEFAULT_FRAME_TITLES, Presentation, Repository, Spec
from mtng.stats import ReleaseStats


@dataclass
class Heading:
    kind: str
    values: dict[str, str]
    template: str
    legacy_template: str | None = None


@dataclass
class ItemList:
    items: list[IssueBase]
    mode: str | None = None
    heading: str | None = None
    actor: str | None = None
    children: list["ItemList"] = field(default_factory=list)
    descriptions: bool = False
    actor_url: str | None = None


@dataclass
class Statistics:
    values: ReleaseStats
    release: str | None


@dataclass
class Agenda:
    contributions: list[dict]


@dataclass
class Notice:
    text: str


@dataclass
class ReportSection:
    id: str
    heading: Heading
    repository: Repository | None
    presentation: Presentation
    blocks: list[ItemList | Statistics | Agenda | Notice]
    # Structural navigation hints; each renderer decides how to display them.
    navigation: str | None = None
    emoji: str | None = None
    navigation_repository: bool = False


@dataclass
class ReportDocument:
    title: str
    since: datetime | None
    now: datetime
    sections: list[ReportSection]
    placeholders: dict[str, str]
    presentation: Presentation = field(default_factory=Presentation)


def merge_presentation(*layers: Presentation) -> Presentation:
    result = {}
    for layer in layers:
        for key, value in layer.model_dump(
            exclude_none=True, include=set(Presentation.model_fields)
        ).items():
            if isinstance(value, dict):
                result[key] = {**result.get(key, {}), **value}
            else:
                result[key] = value
    return Presentation.model_validate(result)


def review_state(
    item,
) -> Literal["unavailable", "changes_requested", "approved", "awaiting"]:
    if not getattr(item, "reviews_available", False):
        return "unavailable"
    latest = {}
    for _, review in sorted(
        enumerate(item.reviews),
        key=lambda pair: (
            pair[1].submitted_at.timestamp() if pair[1].submitted_at else float("-inf"),
            pair[0],
        ),
    ):
        if review.state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[review.user.login] = review.state
    if "CHANGES_REQUESTED" in latest.values():
        return "changes_requested"
    if "APPROVED" in latest.values():
        return "approved"
    return "awaiting"


REVIEW_LABELS = {
    "unavailable": "review unavailable",
    "changes_requested": "changes requested",
    "approved": "approved",
    "awaiting": "awaiting review",
}


def prepare_report(
    spec: Spec, data, since, now, contributions=(), events=None
) -> ReportDocument:
    from mtng.report_text import build_placeholders, expand_placeholders

    placeholders = build_placeholders(data, since, now)
    title = (
        expand_placeholders(spec.title, placeholders)
        or f"Development report for {placeholders['repos']}"
    )
    document = ReportDocument(title, since, now, [], placeholders, spec.presentation)

    def add(repo, section, kind, blocks, category="", navigation=None, emoji=None):
        source = repo["spec"]
        presentation = merge_presentation(
            spec.presentation,
            source.presentation,
            section.presentation if section is not None else Presentation(),
        )
        template = DEFAULT_FRAME_TITLES[kind]
        if source.forge == "gitlab":
            template = template.replace("PRs", "MRs")
        template = presentation.headings.get(kind, template)
        if section is not None and section.title is not None:
            template = section.title
        values = {
            **placeholders,
            "repo": source.display_name or source.name,
            "release": repo.get("release_tag") or "",
            "category": category,
        }
        legacy = (
            source.frame_titles.get(kind)
            if "frame_titles" in source.model_fields_set
            else None
        )
        document.sections.append(
            ReportSection(
                f"section-{len(document.sections) + 1}",
                Heading(kind, values, template, legacy),
                source,
                presentation,
                blocks,
                navigation,
                emoji,
            )
        )

    def items(
        repo,
        section,
        kind,
        values,
        mode=None,
        category="",
        navigation=None,
        emoji=None,
        descriptions=False,
    ):
        if mode == "OPEN":
            values = sorted(values, key=lambda item: item.updated_at, reverse=True)
            values = sorted(values, key=lambda item: item.is_wip)
        elif mode == "MERGED":
            values = sorted(values, key=lambda item: item.closed_at or item.updated_at)
        else:
            values = list(values)
        add(
            repo,
            section,
            kind,
            [ItemList(values, mode, descriptions=descriptions)],
            category,
            navigation,
            emoji,
        )

    def statistics(repo, section):
        stats = repo.get("release_stats")
        if stats is not None and stats.pr_count:
            add(
                repo,
                section,
                "release_stats" if repo.get("release_tag") else "stats",
                [Statistics(stats, repo.get("release_tag"))],
            )

    def changes(repo, section, opened=False):
        source = repo["spec"]
        prefix = "open" if opened else "merged"
        if opened and repo.get("release_tag"):
            return
        values = repo.get(prefix + "_prs", [])
        kind = (
            "open_prs"
            if opened
            else "release_prs" if repo.get("release_tag") else "merged_prs"
        )
        if not values:
            if not opened:
                add(
                    repo,
                    section,
                    kind,
                    [
                        Notice(
                            "No merged requests found"
                            + (
                                f" in release {repo['release_tag']}"
                                if repo.get("release_tag")
                                else f" since {placeholders['since']}"
                            )
                            + "."
                        )
                    ],
                )
            return
        start_index = len(document.sections)
        if not opened:
            if section is None or section.summary:
                statistics(repo, section)
            if source.show_breaking_changes and repo.get("breaking_prs"):
                items(
                    repo,
                    section,
                    "breaking_changes",
                    repo["breaking_prs"],
                    "MERGED",
                    navigation="Breaking changes",
                    emoji="warning",
                )
        groups = repo.get(prefix + "_prs_by_category", [])
        if source.group_prs_by_category and groups:
            for group in groups:
                items(
                    repo,
                    section,
                    kind,
                    group["items"],
                    "OPEN" if opened else "MERGED",
                    group["title"],
                    group["title"],
                    group.get("emoji"),
                )
        else:
            listed = values if opened else repo.get("merged_prs_listed", values)
            if listed:
                items(
                    repo,
                    section,
                    kind,
                    listed,
                    "OPEN" if opened else "MERGED",
                    navigation=(
                        ("Open MRs" if source.forge == "gitlab" else "Open PRs")
                        if opened
                        else (
                            f"Release {repo['release_tag']}"
                            if repo.get("release_tag")
                            else (
                                "Merged MRs"
                                if source.forge == "gitlab"
                                else "Merged PRs"
                            )
                        )
                    ),
                )
                target = document.sections[start_index]
                if target.navigation is None:
                    target.navigation = document.sections[-1].navigation
                    document.sections[-1].navigation = None
                else:
                    target = document.sections[-1]
                target.navigation_repository = True

    def process(repo, section, kind):
        source = repo["spec"]
        if kind == "changes":
            changes(repo, section, section is not None and section.state == "open")
        elif kind == "open":
            changes(repo, section, True)
        elif kind == "statistics":
            statistics(repo, section)
        elif kind == "review_followup":
            if not source.do_review_followup or repo.get("review_followup") is None:
                return
            groups = repo["review_followup"]
            for group in groups:
                child = group.get("rereview")
                actor = group.get("actor")
                actor_url = (
                    actor.html_url
                    if hasattr(actor, "html_url")
                    else actor.get("html_url") if isinstance(actor, dict) else None
                )
                if actor is not None and not isinstance(actor, str):
                    actor = actor.login if hasattr(actor, "login") else actor["login"]
                block = ItemList(
                    list(group["items"]),
                    "OPEN",
                    group["heading"],
                    actor,
                    (
                        [
                            ItemList(
                                list(child["items"]),
                                "OPEN",
                                "Re-review: new commits since last review",
                            )
                        ]
                        if child
                        else []
                    ),
                )
                block.actor_url = actor_url
                add(repo, section, "review_followup", [block])
            if not groups:
                add(
                    repo,
                    section,
                    "review_followup",
                    [Notice("No non-draft PRs need follow-up.")],
                )
        elif kind == "discussion":
            for item in repo.get("needs_discussion", []):
                items(repo, section, "needs_discussion", [item], descriptions=True)
        elif (
            kind == "issues" and not repo.get("release_tag") and source.do_recent_issues
        ):
            values = sorted(
                repo.get("recent_issues", []),
                key=lambda item: item.updated_at,
                reverse=True,
            )
            if values:
                items(repo, section, "recent_issues", values)
            else:
                add(
                    repo,
                    section,
                    "recent_issues",
                    [Notice("No issues opened in this reporting period.")],
                )
        elif kind == "stale" and not repo.get("release_tag") and source.do_stale:
            values = repo.get("stale", [])
            newly = [
                item for item in values if since is not None and item.updated_at > since
            ]
            if newly:
                items(repo, section, "new_stale", newly, "OPEN")
            else:
                add(
                    repo,
                    section,
                    "new_stale",
                    [
                        Notice(
                            "No new stale issues or requests in this reporting period."
                        )
                    ],
                )
            if values:
                # Stale items retain the existing update ordering, without draft partitioning.
                add(
                    repo,
                    section,
                    "all_stale",
                    [
                        ItemList(
                            sorted(
                                values, key=lambda item: item.updated_at, reverse=True
                            ),
                            "OPEN",
                        )
                    ],
                )

    def agenda(title, values):
        if values:
            document.sections.append(
                ReportSection(
                    f"section-{len(document.sections)+1}",
                    Heading("agenda", placeholders, title),
                    None,
                    spec.presentation,
                    [Agenda(list(values))],
                )
            )

    if spec.sections is None:
        for repo in data.values():
            for kind in (
                "discussion",
                "changes",
                "open",
                "review_followup",
                "issues",
                "stale",
            ):
                process(repo, None, kind)
        agenda("Contributions today", contributions)
    else:
        for index, section in enumerate(spec.sections):
            if section.type == "indico":
                agenda(section.title, (events or {}).get(section.event, []))
            else:
                for repo in data.values():
                    if repo.get("section_index") == index:
                        process(repo, section, section.type)
    return document


def heading_text(section, options, latex=False):
    from mtng.report_text import expand_placeholders

    heading = section.heading
    template = heading.template
    if (
        latex
        and heading.legacy_template is not None
        and heading.kind not in section.presentation.headings
    ):
        template = heading.legacy_template
    template = options.headings.get(heading.kind, template)
    return expand_placeholders(template, heading.values)
