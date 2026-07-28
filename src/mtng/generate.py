from datetime import datetime
from pathlib import Path
import re
from typing import Optional
from markupsafe import Markup
from jinja2 import Environment, FileSystemLoader

from mtng.spec import DEFAULT_FRAME_TITLES, Spec


def sanitize(s):
    """Escape LaTeX special characters in a string. Returns a Markup so it is
    safe to apply multiple times (subsequent calls are no-ops)."""
    if isinstance(s, Markup):
        return s

    def repl(m):
        orig = m.group(0)
        return {
            "_": "\\_",
            "%": "\\%",
            "#": "\\#",
            "$": "\\$",
            "&": "\\&",
            "<": "\\textless{}",
            ">": "\\textgreater{}",
            "\\": "\\textbackslash{}",
            "{": "\\{",
            "}": "\\}",
            "^": "\\textasciicircum{}",
        }[orig]

    s = re.sub(r"[\\^_{}&$%#<>]", repl, s)
    return Markup(s)


def human_int(n, dash="--"):
    """Format an integer for display on a statistics tile. Large values are
    abbreviated with a k suffix, smaller ones get thin-space thousands
    separators. Returns a Markup, so chaining with sanitize is a no-op."""
    if n is None:
        return Markup(dash)
    if abs(n) >= 100_000:
        return Markup(f"{n / 1000:.0f}k")
    if abs(n) >= 10_000:
        return Markup(f"{n / 1000:.1f}k")
    return Markup(f"{n:,}".replace(",", "\\,"))


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
            repo["spec"].display_name or name for name, repo in data.items()
        ),
        "since": since.strftime("%Y-%m-%d") if since is not None else "",
        "date": now.strftime("%Y-%m-%d"),
        "range": (
            f"between {since.strftime('%Y-%m-%d')} and {now.strftime('%Y-%m-%d')}"
            if since is not None
            else ""
        ),
    }


def expand_placeholders(
    text: Optional[str], values: dict, escape: bool = False
) -> Optional[str]:
    """Substitute {keyword} occurrences. Unknown keywords are left untouched, and
    a keyword that expands to nothing takes any dangling separator or empty
    bracket pair with it. With `escape`, the literal text around the
    placeholders is sanitized while the substituted values are inserted as is,
    so a value may carry LaTeX of its own."""
    if text is None:
        return None

    out = []
    last = 0
    for m in PLACEHOLDER_RE.finditer(text):
        if m.group(1) not in values:
            continue
        chunk = text[last : m.start()]
        out.append(sanitize(chunk) if escape else chunk)
        out.append(str(values[m.group(1)]))
        last = m.end()
    tail = text[last:]
    out.append(sanitize(tail) if escape else tail)

    expanded = "".join(out)
    if last != 0:  # something was substituted, so tidy up after empty values
        expanded = EMPTY_GROUP_RE.sub("", expanded)
        expanded = DANGLING_SEPARATOR_RE.sub("", re.sub(r"\s{2,}", " ", expanded))
    expanded = expanded.strip()
    return Markup(expanded) if escape else expanded


def make_frame_title(placeholders: dict):
    """Jinja helper resolving a repository's configured title for one kind of
    frame. Values are sanitized here, the configured template around them too."""

    def frame_title(kind: str, repo, category: Optional[str] = None) -> str:
        spec = repo["spec"]
        template = spec.frame_titles.get(kind, DEFAULT_FRAME_TITLES[kind])
        values = dict(placeholders)
        values.update(
            {
                # The repository name is already bound to a macro by repo.tex.
                "repo": Markup(r"\reponame{}"),
                "release": sanitize(repo["release_tag"] or ""),
                "category": sanitize(category or ""),
            }
        )
        return expand_placeholders(template, values, escape=True)

    return frame_title


env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "template"),
)

env.filters["sanitize"] = sanitize
env.filters["human_int"] = human_int

# Comparison tests missing from Jinja2 defaults
env.tests["gt"] = lambda value, other: value > other
env.tests["ge"] = lambda value, other: value >= other
env.tests["lt"] = lambda value, other: value < other
env.tests["le"] = lambda value, other: value <= other

env.globals["include_raw"] = lambda q: Markup(env.loader.get_source(env, q)[0])


def generate_latex(
    spec: Spec, data, since: datetime, now: datetime, contributions, full_tex: bool
) -> str:
    tpl = env.get_template("main.tex")

    placeholders = build_placeholders(data, since, now)

    return tpl.render(
        repos=data,
        spec=spec,
        deck_title=expand_placeholders(spec.title, placeholders),
        footline_left=expand_placeholders(spec.footline_left, placeholders),
        frame_title=make_frame_title(placeholders),
        since=since,
        now=now,
        contributions=contributions,
        full_tex=full_tex,
    ).strip()
