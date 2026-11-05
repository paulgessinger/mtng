from datetime import datetime, timedelta
from pathlib import Path
import re
import humanize
from markupsafe import Markup
from jinja2 import Environment, FileSystemLoader

from mtng.spec import DEFAULT_FRAME_TITLES, Spec
from mtng.report_text import (
    build_placeholders,
    expand_placeholders as expand_report_text,
)


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


def expand_placeholders(text: str | None, values: dict, escape: bool = False):
    """Compatibility wrapper adding LaTeX escaping to plain placeholder expansion."""
    expanded = expand_report_text(text, values, sanitize if escape else str)
    return Markup(expanded) if escape and expanded is not None else expanded


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
    from mtng.document import REVIEW_LABELS, review_state

    state = review_state(item)
    return (
        REVIEW_LABELS[state],
        {
            "unavailable": "black!55",
            "changes_requested": "red!75!black",
            "approved": "green!50!black",
            "awaiting": "black!55",
        }[state],
    )


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
    from mtng.document import prepare_report

    document = prepare_report(spec, data, since, now, contributions, events)
    return render_latex(document, spec, full_tex)


def render_latex(document, spec, full_tex=False):
    """Adapt semantic blocks to the existing Beamer item and statistics styling."""
    from mtng.document import Statistics, merge_presentation

    options = spec.output.latex
    hex_colors = {}

    def color_name(color):
        if re.fullmatch(r"#?[0-9a-fA-F]{6}", color):
            value = color.lstrip("#").upper()
            name = "mtngcolor" + value
            hex_colors[name] = value
            return name
        return color

    def presentation(section):
        return merge_presentation(section.presentation, options)

    def latex_repo(section):
        if section.repository is None:
            return None
        source = section.repository.model_copy(deep=True)
        display = presentation(section)
        if options.truncate_titles is not None:
            source.truncate_pr_titles = options.truncate_titles
        if display.show_categories is not None:
            source.show_pr_categories = display.show_categories
        if display.metadata is not None:
            source.do_assignee = "assignees" in display.metadata
            source.do_reviewers = "review_status" in display.metadata
            source.show_review_summary = source.do_reviewers
        source.pr_category_colors.update(
            {
                key: color_name(value)
                for key, value in display.colors.items()
                if key != "accent"
            }
        )
        source.pr_category_colors = {
            key: color_name(value) for key, value in source.pr_category_colors.items()
        }
        stats = next(
            (block for block in section.blocks if isinstance(block, Statistics)), None
        )
        return {
            "spec": source,
            "release_tag": section.heading.values.get("release") or None,
            "release_stats": stats.values if stats else None,
            "section": section,
        }

    def latex_heading(section):
        heading = section.heading
        template = heading.template
        if (
            heading.legacy_template is not None
            and heading.kind not in section.presentation.headings
        ):
            template = heading.legacy_template
        template = options.headings.get(heading.kind, template)
        values = {key: sanitize(value) for key, value in heading.values.items()}
        if section.repository is not None:
            values["repo"] = Markup(r"\reponame{}")
        return expand_placeholders(template, values, escape=True)

    def latex_item(item, section):
        item = item.model_copy(deep=True)
        display = presentation(section)
        if item.pr_category in display.colors:
            item.pr_category_color = color_name(display.colors[item.pr_category])
        elif item.pr_category_color:
            item.pr_category_color = color_name(item.pr_category_color)
        if display.show_summaries is False:
            item.llm_summary = None
        if display.metadata is not None and "diff" not in display.metadata:
            for key in ("additions", "deletions", "changed_files"):
                if hasattr(item, key):
                    setattr(item, key, None)
        return item

    def show_metadata(section, field):
        fields = presentation(section).metadata
        return fields is None or field in fields

    # Define hex colors before any item uses them, including in fragment output.
    for section in document.sections:
        latex_repo(section)

        def visit(block):
            for item in getattr(block, "items", []):
                latex_item(item, section)
            for child in getattr(block, "children", []):
                visit(child)

        for block in section.blocks:
            visit(block)

    display_spec = spec.model_copy(deep=True)
    display_spec.accent_color = options.colors.get(
        "accent", spec.presentation.colors.get("accent", spec.accent_color)
    ).lstrip("#")
    rendered = (
        env.get_template("document.tex")
        .render(
            document=document,
            spec=display_spec,
            full_tex=full_tex,
            deck_title=expand_placeholders(spec.title, document.placeholders),
            footline_left=expand_placeholders(
                (
                    options.footline_left
                    if options.footline_left is not None
                    else spec.footline_left
                ),
                document.placeholders,
            ),
            title_repositories=list(
                dict.fromkeys(
                    repo.display_name or repo.name for repo in spec.source_repositories
                )
            ),
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
            since=document.since,
            now=document.now,
            latex_repo=latex_repo,
            latex_heading=latex_heading,
            latex_item=latex_item,
            hex_colors=hex_colors,
            show_metadata=show_metadata,
            frame_title=lambda kind, repo: latex_heading(repo["section"]),
        )
        .strip()
    )

    return re.sub(
        r"\n{3,}", "\n\n", "\n".join(line.rstrip() for line in rendered.splitlines())
    ).strip()
