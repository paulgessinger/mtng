from datetime import datetime, timedelta
from pathlib import Path
import re
import humanize
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


def expand_placeholders(
    text: str | None, values: dict, escape: bool = False
) -> str | None:
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

    def frame_title(kind: str, repo, category: str | None = None) -> str:
        spec = repo["spec"]
        template = spec.frame_titles.get(kind, DEFAULT_FRAME_TITLES[kind])
        if spec.forge == "gitlab" and (
            "frame_titles" not in spec.model_fields_set or kind not in spec.frame_titles
        ):
            template = template.replace("PRs", "MRs")
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


def title_characters(value: str) -> Markup:
    """Group escaped characters so TeX truncation cannot split escape commands."""
    return Markup("".join("{" + str(sanitize(char)) + "}" for char in value))


def title_units(value: str) -> Markup:
    """Prefer whole words; retain character truncation for short/token-only titles."""
    words = re.findall(r"\S+\s*", value)
    if len(words) < 3:
        return title_characters(value)
    return Markup("".join("{" + str(sanitize(word)) + "}" for word in words))


def age_days(value: datetime, now: datetime) -> int:
    # Compare calendar days in the report timezone, independent of render time.
    if value.tzinfo is not None and now.tzinfo is not None:
        value = value.astimezone(now.tzinfo)
    return max(0, (now.date() - value.date()).days)


def human_days(days: int) -> str:
    """Readable ages without rounding a few weeks up to a whole month."""
    return humanize.naturaldelta(timedelta(days=days), months=False)


def review_status(item) -> tuple[str, str]:
    if not getattr(item, "reviews_available", False):
        return "review unavailable", "black!55"
    latest = {}
    reviews = sorted(
        enumerate(item.reviews),
        key=lambda pair: (
            pair[1].submitted_at.timestamp() if pair[1].submitted_at else float("-inf"),
            pair[0],
        ),
    )
    for _, review in reviews:
        if review.state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[review.user.login] = review.state
    if "CHANGES_REQUESTED" in latest.values():
        return "changes requested", "red!75!black"
    if "APPROVED" in latest.values():
        return "approved", "green!50!black"
    return "awaiting review", "black!55"


env.filters["sanitize"] = sanitize
env.filters["title_characters"] = title_characters
env.filters["title_units"] = title_units
env.filters["age_days"] = age_days
env.filters["human_days"] = human_days
env.filters["review_status"] = review_status
env.filters["human_int"] = human_int

# Comparison tests missing from Jinja2 defaults
env.tests["gt"] = lambda value, other: value > other
env.tests["ge"] = lambda value, other: value >= other
env.tests["lt"] = lambda value, other: value < other
env.tests["le"] = lambda value, other: value <= other

env.globals["include_raw"] = lambda q: Markup(env.loader.get_source(env, q)[0])


def generate_latex(
    spec: Spec,
    data,
    since: datetime,
    now: datetime,
    contributions,
    full_tex: bool,
    events=None,
) -> str:
    tpl = env.get_template("main.tex" if spec.sections is None else "sections.tex")

    placeholders = build_placeholders(data, since, now)

    return tpl.render(
        request_label=(
            "Merge request"
            if spec.source_repositories
            and all(repo.forge == "gitlab" for repo in spec.source_repositories)
            else (
                "Pull/merge request"
                if any(repo.forge == "gitlab" for repo in spec.source_repositories)
                else "Pull request"
            )
        ),
        title_repositories=list(
            dict.fromkeys(
                repo["spec"].display_name or repo["spec"].name for repo in data.values()
            )
        ),
        repos=data,
        events=events or {},
        spec=spec,
        deck_title=expand_placeholders(spec.title, placeholders),
        footline_left=expand_placeholders(spec.footline_left, placeholders),
        frame_title=make_frame_title(placeholders),
        since=since,
        now=now,
        contributions=contributions,
        full_tex=full_tex,
    ).strip()
