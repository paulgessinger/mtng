from datetime import datetime
from pathlib import Path
from jinja2 import Environment, FileSystemLoader

from mtng.spec import Spec


def generate_latex(
    spec: Spec, data, last: datetime, contributions, full_tex: bool
) -> str:

    env = Environment(
        loader=FileSystemLoader(Path(__file__).parent / "template"),
    )

    def sanitize(s):
        return s.replace("_", "\\_").replace("#", "\\#").replace("&", "\\&")

    env.filters["sanitize"] = sanitize

    tpl = env.get_template("main.tex")

    return tpl.render(
        repos=data, spec=spec, last=last, contributions=contributions, full_tex=full_tex
    )
