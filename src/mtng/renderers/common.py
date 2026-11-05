from urllib.parse import urlsplit

from mtng.document import REVIEW_LABELS, merge_presentation, review_state


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
    if (
        item.is_pr
        and document.since is not None
        and document.since <= item.created_at <= document.now
    ):
        badges.append("new")
    if item.pr_category_breaking:
        badges.append("breaking")
    if item.draft:
        badges.append("draft")
    elif item.is_wip:
        badges.append("WIP")
    if item.is_stale:
        badges.append("stale")
    show_categories = presentation.show_categories
    if show_categories is None:
        show_categories = source.show_pr_categories
    if show_categories and item.pr_category_label:
        badges.append(item.pr_category_label)
    if "author" in fields:
        metadata.append(f"by @{item.user.login}")
    if "updated" in fields:
        date = item.closed_at if block.mode == "MERGED" else item.updated_at
        if date is not None:
            metadata.append(
                f"{'merged' if block.mode == 'MERGED' else 'updated'} {date:%Y-%m-%d}"
            )
    if "assignees" in fields:
        users = item.assignees or ([item.assignee] if item.assignee else [])
        metadata.append(
            "assigned to " + ", ".join("@" + user.login for user in users)
            if users
            else "no assignee"
        )
    if "review_status" in fields and hasattr(item, "reviews_available"):
        metadata.append(REVIEW_LABELS[review_state(item)])
    if "diff" in fields:
        if (
            getattr(item, "additions", None) is not None
            and getattr(item, "deletions", None) is not None
        ):
            metadata.append(f"+{item.additions}/-{item.deletions}")
        if getattr(item, "changed_files", None) is not None:
            metadata.append(f"{item.changed_files} files")
    prefix = "!" if item.is_pr and source.forge == "gitlab" else "#"
    return {
        "title": item.pr_title_display or item.title,
        "reference": f"{prefix}{item.number}",
        "url": safe_url(item.html_url),
        "badges": badges,
        "metadata": metadata,
        "summary": (
            item.llm_summary if presentation.show_summaries is not False else None
        ),
        "description": item.body if block.descriptions else None,
        "color": presentation.colors.get(item.pr_category, ""),
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
