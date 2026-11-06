"""Shared, renderer-independent report titles and placeholder expansion."""

import re
from datetime import datetime

PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
# Leftovers when a placeholder expands to nothing, e.g. "Core team -- {release}"
# or "{repo}: Open PRs ({category})".
DANGLING_SEPARATOR_RE = re.compile(r"[\s]*[-–—:|,]+[\s]*$|^[\s]*[-–—:|,]+[\s]*")
EMPTY_GROUP_RE = re.compile(r"\s*[(\[]\s*[)\]]")


def build_placeholders(data, since: datetime, now: datetime) -> dict:
    """Values available as {keyword} in user-supplied deck strings."""
    tags = []
    for repo in data.values():
        tag = repo.get("release_tag")
        if tag is not None and tag not in tags:
            tags.append(tag)
    return {
        "release": ", ".join(tags),
        "repos": ", ".join(
            dict.fromkeys(
                repo["spec"].display_name or repo["spec"].name for repo in data.values()
            )
        ),
        "since": since.strftime("%Y-%m-%d") if since is not None else "",
        "date": now.strftime("%Y-%m-%d"),
        "range": (
            f"between {since.strftime('%Y-%m-%d')} and {now.strftime('%Y-%m-%d')}"
            if since is not None
            else ""
        ),
    }


def expand_placeholders(text: str | None, values: dict, transform=str) -> str | None:
    """Expand known placeholders, tidying separators left by empty values.

    The optional transform escapes literal template text for a renderer. Values
    must already be escaped by the caller when using a transform.
    """
    if text is None:
        return None

    out = []
    last = 0
    for m in PLACEHOLDER_RE.finditer(text):
        if m.group(1) not in values:
            continue
        chunk = text[last : m.start()]
        out.append(transform(chunk))
        out.append(str(values[m.group(1)]))
        last = m.end()
    tail = text[last:]
    out.append(transform(tail))

    expanded = "".join(out)
    if last != 0:  # something was substituted, so tidy up after empty values
        expanded = EMPTY_GROUP_RE.sub("", expanded)
        expanded = DANGLING_SEPARATOR_RE.sub("", re.sub(r"\s{2,}", " ", expanded))
    expanded = expanded.strip()
    return expanded
