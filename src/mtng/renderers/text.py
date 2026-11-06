"""Independent Markdown and plain-text serialization of the same report blocks."""

import re
import textwrap
import unicodedata
from urllib.parse import quote

from mtng.document import Agenda, ItemList, Notice, Statistics
from .common import (
    BADGE_EMOJI,
    REVIEW_EMOJI,
    emoji,
    item_content,
    metrics,
    safe_url,
    section_groups,
)


def markdown_escape(value):
    # Escape syntax, while keeping ordinary dates, prose and usernames readable.
    text = re.sub(
        r"([\\`*_{}\[\]<>|#~&])",
        r"\\\1",
        str(value).replace("\n", " ").replace("\r", " "),
    )
    # A notice or description may begin with a list marker.
    return re.sub(
        r"^(\s*)([-+]|[0-9]+[.)])(?=\s)",
        lambda match: match[1] + match[2][:-1] + "\\" + match[2][-1],
        text,
    )


def _render(document, options, markdown):
    lines = []
    references = {}
    escape = (
        markdown_escape
        if markdown
        else lambda value: str(value).replace("\n", " ").replace("\r", " ")
    )

    def link(label, url):
        label = escape(label)
        url = safe_url(url)
        if not url or (not markdown and options.links == "omit"):
            return label
        if markdown:
            url = quote(url, safe="/:?=&%#@+;,$!~*'-._")
        if options.links in {"reference", "footnotes"}:
            number = references.setdefault(url, len(references) + 1)
            return f"[{label}][{number}]" if markdown else f"{label} [{number}]"
        return f"[{label}](<{url}>)" if markdown else f"{label} ({url})"

    # Plain text stays plain; Markdown decorates unless disabled.
    decorate = markdown and options.emoji

    def icon(value):
        return value + " " if decorate and value else ""

    def badge_text(row, badge):
        if badge["kind"] == "category":
            return icon(row["category_emoji"]) + badge["label"]
        return icon(emoji(BADGE_EMOJI.get(badge["kind"]))) + badge["label"]

    def badge_markdown(row, badge):
        if decorate and badge["kind"] in ("new", "breaking"):
            return emoji(BADGE_EMOJI[badge["kind"]])
        return "`" + badge_text(row, badge).replace("`", "") + "`"

    def meta_text(entry):
        if entry["kind"] == "review":
            return icon(REVIEW_EMOJI[entry["state"]]) + escape(entry["text"])
        if entry.get("attention"):
            return icon(emoji("warning")) + escape(entry["text"])
        if markdown and entry.get("users"):
            return escape(entry["prefix"]) + ", ".join(
                link("@" + user["login"], user["url"]) for user in entry["users"]
            )
        return escape(entry["text"])

    def actor(block):
        if not block.actor:
            return ""
        if markdown:
            return ": " + link("@" + block.actor, block.actor_url or "")
        return f": @{block.actor}"

    def heading(value, level, suffix=""):
        lines.extend(
            [("#" * level + " " if markdown else "") + escape(value) + suffix, ""]
        )

    base = options.heading_level if markdown else 1
    heading(document.title, base)
    if document.since:
        lines.extend([f"{document.since:%Y-%m-%d} – {document.now:%Y-%m-%d}", ""])
    else:
        lines.extend([f"{document.now:%Y-%m-%d}", ""])

    def render_items(block, section):
        if block.heading:
            heading(block.heading, base + 2, actor(block))
        for item in block.items:
            row = item_content(item, block, section, document, options)
            text = link(f"{row['title']} ({row['reference']})", row["url"])
            if markdown:
                # Badges lead the title, as on the slides; code spans read as pills.
                if decorate:
                    text = f"**{text}**"
                text = " ".join(
                    [*(badge_markdown(row, badge) for badge in row["badges"]), text]
                )
            elif row["badges"]:
                text += " — " + escape(
                    ", ".join(badge_text(row, badge) for badge in row["badges"])
                )
            if markdown:
                # Title, metadata and summary on their own lines within the item;
                # a trailing backslash is CommonMark's portable hard line break.
                item_lines = [text]
                if row["metadata"]:
                    item_lines.append(" · ".join(map(meta_text, row["metadata"])))
                if row["summary"]:
                    item_lines.append(icon(emoji("robot")) + escape(row["summary"]))
                lines.append("- " + "\\\n  ".join(item_lines))
            else:
                if row["metadata"]:
                    text += "; " + "; ".join(map(meta_text, row["metadata"]))
                lines.append("- " + text)
                if row["summary"]:
                    lines.append("  " + escape(row["summary"]))
            if row["description"]:
                lines.extend(["", "  " + escape(row["description"])])
        lines.append("")
        for child in block.children:
            render_items(child, section)

    for group in section_groups(document, options):
        heading(icon(emoji(group.emoji)) + group.heading, base + 1)
        for section, block, _ in group.parts:
            if isinstance(block, ItemList):
                render_items(block, section)
            elif isinstance(block, Notice):
                lines.extend([escape(block.text), ""])
            elif isinstance(block, Statistics):
                if markdown and options.statistics == "table":
                    lines.extend(["| Metric | Value |", "| --- | --- |"])
                    lines.extend(
                        f"| {escape(key)} | {escape(value)} |"
                        for key, value in metrics(block)
                    )
                else:
                    lines.extend(
                        f"- {escape(key)}: {escape(value)}"
                        for key, value in metrics(block)
                    )
                lines.append("")
            elif isinstance(block, Agenda):
                for contribution in block.contributions:
                    text = link(contribution["title"], contribution["url"])
                    if contribution.get("speakers"):
                        text += " — " + escape(", ".join(contribution["speakers"]))
                    lines.append("- " + text)
                lines.append("")
    for url, number in references.items():
        lines.append(f"[{number}]: <{url}>" if markdown else f"[{number}] {url}")
    result = "\n".join(lines).strip() + "\n"
    if not markdown:
        if not options.unicode:
            result = result.translate(
                str.maketrans(
                    {"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"'}
                )
            )
            result = (
                unicodedata.normalize("NFKD", result)
                .encode("ascii", "replace")
                .decode()
            )
        if options.width:
            result = (
                "\n".join(
                    (
                        textwrap.fill(
                            line,
                            width=options.width,
                            subsequent_indent=(
                                "  " if line.startswith(("- ", "  ")) else ""
                            ),
                            break_long_words=False,
                            break_on_hyphens=False,
                        )
                        if line
                        else ""
                    )
                    for line in result.splitlines()
                )
                + "\n"
            )
    return result


def render_markdown(document, options):
    return _render(document, options, True)


def render_text(document, options):
    return _render(document, options, False)
