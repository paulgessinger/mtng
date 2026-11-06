from urllib.parse import urlsplit

from dataclasses import dataclass, field

from mtng.document import (
    REVIEW_LABELS,
    ReportSection,
    heading_text,
    merge_presentation,
    review_state,
)
from mtng.spec import DEFAULT_PR_CATEGORY_COLORS, DEFAULT_PR_CATEGORY_EMOJI

# Unicode for the CLDR short names the LaTeX templates pass to the `emoji` package.
EMOJI = {
    "rocket": "🚀",
    "bug": "🐛",
    "high-voltage": "⚡",
    "recycling-symbol": "♻️",
    "books": "📚",
    "test-tube": "🧪",
    "hammer": "🔨",
    "robot": "🤖",
    "gear": "⚙️",
    "counterclockwise-arrows-button": "🔄",
    "package": "📦",
    "warning": "⚠️",
    "new-button": "🆕",
    "police-car-light": "🚨",
    "construction": "🚧",
    "memo": "📝",
    "spider-web": "🕸️",
}

BADGE_EMOJI = {
    "new": "new-button",
    "breaking": "police-car-light",
    "draft": "memo",
    "wip": "construction",
    "stale": "spider-web",
}

REVIEW_EMOJI = {
    "unavailable": "",
    "changes_requested": "❌",
    "approved": "✅",
    "awaiting": "⏳",
}

# Hex values of the LaTeX color names used by the default category palette, for
# formats that cannot resolve LaTeX colors.
LATEX_COLORS = {
    "LimeGreen": "#87C945",
    "Purple": "#8F45A3",
    "TealBlue": "#1FAEB0",
    "CadetBlue": "#5F7AA5",
    "CornflowerBlue": "#41A6E0",
    "RoyalBlue": "#0071BC",
    "Goldenrod": "#F2C12E",
    "Orange": "#F58137",
    "Gray": "#8A8C8E",
    "Mahogany": "#A9341F",
    "beige": "#FFEBC6",
    "alertred": "#EF5D60",
}


def emoji(name):
    return EMOJI.get(name or "", "")


def category_color(category, presentation, source):
    """A hex color for a category: presentation colors first, then the
    repository's LaTeX palette when it uses known color names."""
    if category in presentation.colors:
        return presentation.colors[category]
    name = source.pr_category_colors.get(
        category, DEFAULT_PR_CATEGORY_COLORS.get(category)
    )
    return LATEX_COLORS.get(name, "")


def contrast_text(color):
    """Black or white, whichever reads better on the given hex background."""
    red, green, blue = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return "#1C1C1E" if 0.299 * red + 0.587 * green + 0.114 * blue > 150 else "#FFFFFF"


def safe_url(value):
    """Only web links are meaningful in collected reports."""
    try:
        parsed = urlsplit(value)
        return (
            value
            if parsed.scheme.lower() in {"http", "https"} and parsed.netloc
            else ""
        )
    except ValueError:
        return ""


def item_content(item, block, section, document, options):
    presentation = merge_presentation(section.presentation, options)
    source = section.repository
    fields = presentation.metadata
    if fields is None:
        fields = ["author", "updated", "diff"]
        if source.do_assignee:
            fields.append("assignees")
        if source.do_reviewers and source.show_review_summary:
            fields.append("review_status")
    metadata = []
    badges = []

    def badge(kind, label, color=""):
        badges.append({"kind": kind, "label": label, "color": color})

    def meta(kind, text, **extra):
        metadata.append({"kind": kind, "text": text, **extra})

    if (
        item.is_pr
        and document.since is not None
        and document.since <= item.created_at <= document.now
    ):
        badge("new", "new")
    if item.pr_category_breaking:
        badge("breaking", "breaking")
    if item.draft:
        badge("draft", "draft")
    elif item.is_wip:
        badge("wip", "WIP")
    if item.is_stale:
        badge("stale", "stale")
    show_categories = presentation.show_categories
    if show_categories is None:
        show_categories = source.show_pr_categories
    category = item.pr_category or "other"
    color = category_color(category, presentation, source)
    if show_categories and item.pr_category_label:
        badge("category", item.pr_category_label, color)

    def people(users):
        return [{"login": user.login, "url": safe_url(user.html_url)} for user in users]

    if "author" in fields:
        meta(
            "author", f"by @{item.user.login}", prefix="by ", users=people([item.user])
        )
    if "updated" in fields:
        date = item.closed_at if block.mode == "MERGED" else item.updated_at
        if date is not None:
            meta(
                "updated",
                f"{'merged' if block.mode == 'MERGED' else 'updated'} {date:%Y-%m-%d}",
            )
    if "assignees" in fields:
        users = item.assignees or ([item.assignee] if item.assignee else [])
        meta(
            "assignees",
            (
                "assigned to " + ", ".join("@" + user.login for user in users)
                if users
                else "no assignee"
            ),
            prefix="assigned to " if users else "",
            users=people(users),
            attention=not users
            and source.no_assignee_attention
            and block.mode != "MERGED",
        )
    if "review_status" in fields and hasattr(item, "reviews_available"):
        state = review_state(item)
        meta("review", REVIEW_LABELS[state], state=state)
    if "diff" in fields:
        if (
            getattr(item, "additions", None) is not None
            and getattr(item, "deletions", None) is not None
        ):
            meta(
                "diff",
                f"+{item.additions}/-{item.deletions}",
                additions=item.additions,
                deletions=item.deletions,
            )
        if getattr(item, "changed_files", None) is not None:
            meta("files", f"{item.changed_files} files")
    prefix = "!" if item.is_pr and source.forge == "gitlab" else "#"
    if not item.is_pr:
        state = "issue"
    elif block.mode == "MERGED":
        state = "merged"
    elif item.draft:
        state = "draft"
    else:
        state = "open"
    return {
        "state": state,
        "category_emoji": (
            emoji(DEFAULT_PR_CATEGORY_EMOJI.get(category))
            if show_categories and item.pr_category_label
            else ""
        ),
        "title": item.pr_title_display or item.title,
        "reference": f"{prefix}{item.number}",
        "url": safe_url(item.html_url),
        "badges": badges,
        "metadata": metadata,
        "summary": (
            item.llm_summary if presentation.show_summaries is not False else None
        ),
        "description": item.body if block.descriptions else None,
        "color": color,
    }


def metrics(block):
    stats = block.values
    values = [
        ("Merged requests", stats.pr_count),
        ("Contributors", stats.contributors),
        ("Reviewers", stats.reviewers),
    ]
    if block.release:
        values.insert(0, ("Release", block.release))
    if stats.has_churn:
        values.extend(
            (label, getattr(stats, key))
            for label, key in (
                ("Lines added", "additions"),
                ("Lines removed", "deletions"),
                ("Files changed", "changed_files"),
                ("Commits", "commits"),
            )
        )
    if stats.has_span:
        values.append(("Days", stats.span_days))
    if stats.top_labels:
        values.append(
            (
                "Top labels",
                ", ".join(f"{label.name}: {label.count}" for label in stats.top_labels),
            )
        )
    if not stats.churn_complete:
        values.append(
            (
                "Note",
                "Change totals are incomplete (some requests have no change statistics).",
            )
        )
    return [
        (name, "unavailable" if value is None else str(value)) for name, value in values
    ]


@dataclass
class SectionGroup:
    """Consecutive sections sharing a heading, e.g. one per reviewer for review
    follow-up. Slides keep them apart for pagination; documents show them once."""

    id: str
    heading: str
    emoji: str | None
    # (section, block, anchor) in document order.
    parts: list[tuple[ReportSection, object, str]] = field(default_factory=list)
    # (anchor, text) of the block headings inside the group, for navigation.
    subheadings: list[tuple[str, str]] = field(default_factory=list)


def section_groups(document, options):
    groups = []
    for section in document.sections:
        heading = heading_text(section, options)
        if not groups or groups[-1].heading != heading:
            groups.append(SectionGroup(section.id, heading, section.emoji))
        group = groups[-1]
        for block in section.blocks:
            anchor = f"{group.id}-{len(group.parts)}"
            group.parts.append((section, block, anchor))
    for group in groups:
        # One navigation entry per block heading, linking to its first block;
        # per-reviewer blocks collapse into a count.
        entries = {}
        for _, block, anchor in group.parts:
            if heading := getattr(block, "heading", None):
                entries.setdefault(heading, [anchor, 0])[1] += 1
        if len(entries) > 1:
            group.subheadings = [
                (anchor, f"{heading} ({count})" if count > 1 else heading)
                for heading, (anchor, count) in entries.items()
            ]
    return groups
