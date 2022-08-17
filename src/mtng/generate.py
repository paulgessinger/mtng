from datetime import datetime
from pathlib import Path
import re
from jinja2 import Environment, FileSystemLoader

from mtng.spec import Spec

env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "template"),
)


def sanitize(s):
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

    s = re.sub(r"[\\^_{}&$%#<>%]", repl, s)
    return s


env.filters["sanitize"] = sanitize

env.globals["include_raw"] = lambda q: env.loader.get_source(env, q)[0]


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
