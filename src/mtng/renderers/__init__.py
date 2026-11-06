"""Render prepared reports. Renderers return strings and never write output files."""

from .text import render_markdown, render_text
from .html import render_html

RENDERERS = {"html": render_html, "markdown": render_markdown, "text": render_text}
