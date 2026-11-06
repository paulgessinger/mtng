from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from mtng.document import heading_text
from .common import (
    BADGE_EMOJI,
    REVIEW_EMOJI,
    contrast_text,
    emoji,
    item_content,
    metrics,
    safe_url,
    section_groups,
)

env = Environment(
    loader=FileSystemLoader(Path(__file__).parent.parent / "template"),
    autoescape=select_autoescape(["html"]),
)


def render_html(document, options):
    stylesheet = options.stylesheet.read_text() if options.stylesheet else ""
    # CSS is trusted local configuration, but it must not terminate the style element.
    stylesheet = stylesheet.replace("<", r"\3c ")
    return (
        env.get_template("report.html")
        .render(
            document=document,
            options=options,
            heading_text=heading_text,
            item_content=item_content,
            metrics=metrics,
            safe_url=safe_url,
            section_groups=section_groups,
            emoji=emoji,
            contrast_text=contrast_text,
            badge_emoji=BADGE_EMOJI,
            review_emoji=REVIEW_EMOJI,
            stylesheet=stylesheet,
            accent=options.colors.get(
                "accent", document.presentation.colors.get("accent", "#22529E")
            ),
        )
        .strip()
        + "\n"
    )
