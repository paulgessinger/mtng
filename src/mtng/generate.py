from datetime import datetime
from pathlib import Path
import re
from markupsafe import Markup
from jinja2 import Environment, FileSystemLoader

from mtng.spec import Spec


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


env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "template"),
)

env.filters["sanitize"] = sanitize

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

    return tpl.render(
        repos=data,
        spec=spec,
        since=since,
        now=now,
        contributions=contributions,
        full_tex=full_tex,
    ).strip()
