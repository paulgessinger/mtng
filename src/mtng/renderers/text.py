"""Independent Markdown and plain-text serialization of the same report blocks."""

import re
import textwrap
import unicodedata
from urllib.parse import quote

from mtng.document import Agenda, ItemList, Notice, Statistics, heading_text
from .common import item_content, metrics, safe_url


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

    def heading(value, level):
        lines.extend([("#" * level + " " if markdown else "") + escape(value), ""])

    base = options.heading_level if markdown else 1
    heading(document.title, base)
    if document.since:
        lines.extend([f"{document.since:%Y-%m-%d} – {document.now:%Y-%m-%d}", ""])
    else:
        lines.extend([f"{document.now:%Y-%m-%d}", ""])

    def render_items(block, section):
        if block.heading:
            heading(
                block.heading + (f": @{block.actor}" if block.actor else ""), base + 2
            )
        for item in block.items:
            row = item_content(item, block, section, document, options)
            text = link(f"{row['title']} ({row['reference']})", row["url"])
            if row["badges"]:
                text += " — " + escape(", ".join(row["badges"]))
            if row["metadata"]:
                text += "; " + escape("; ".join(row["metadata"]))
            lines.append("- " + text)
            if row["summary"]:
                lines.append("  " + escape(row["summary"]))
            if row["description"]:
                lines.extend(["", "  " + escape(row["description"])])
        lines.append("")
        for child in block.children:
            render_items(child, section)

    for section in document.sections:
        heading(heading_text(section, options), base + 1)
        for block in section.blocks:
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
