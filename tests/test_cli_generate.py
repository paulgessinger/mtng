import json
import re
from datetime import datetime
from pathlib import Path

from typer.testing import CliRunner

import mtng.cli

# Strip ANSI escape codes so assertions on error text survive rich's
# colorization: in a color-capable environment (e.g. CI) rich highlights
# option names, splitting "--preamble" into escape-wrapped fragments that
# defeat a plain substring match.
_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _plain(text: str) -> str:
    return _ANSI_RE.sub("", text)


def test_generate_rejects_preamble_with_full(tmp_path):
    runner = CliRunner()
    spec = tmp_path / "spec.yml"
    spec.write_text("repos: []\n")
    preamble = tmp_path / "preamble.tex"
    preamble.write_text("% custom preamble\n")

    result = runner.invoke(
        mtng.cli.cli,
        [
            "generate",
            str(spec),
            "--since",
            "2024-01-01",
            "--full",
            "--preamble",
            str(preamble),
        ],
    )

    assert result.exit_code != 0
    assert "--preamble cannot be used with --full." in _plain(result.output)


def test_generate_accepts_human_readable_datetime(monkeypatch, tmp_path):
    runner = CliRunner()
    spec = tmp_path / "spec.yml"
    spec.write_text("repos: []\n")
    captured = {}

    async def fake_collect_repositories(*args, **kwargs):
        captured["since"] = kwargs["since"]
        captured["now"] = kwargs["now"]
        return {}

    monkeypatch.setattr("mtng.cli.resolve_github_token", lambda token: "token")
    monkeypatch.setattr("mtng.cli.collect_repositories", fake_collect_repositories)
    monkeypatch.setattr("mtng.cli.generate_latex", lambda *args, **kwargs: "")

    result = runner.invoke(
        mtng.cli.cli,
        [
            "generate",
            str(spec),
            "--since",
            "1 week ago",
            "--now",
            "now",
        ],
    )

    assert result.exit_code == 0
    assert isinstance(captured["since"], datetime)
    assert isinstance(captured["now"], datetime)
    assert captured["since"].tzinfo is not None
    assert captured["now"].tzinfo is not None
    assert captured["since"] <= captured["now"]


def test_generate_rejects_unparseable_datetime(monkeypatch, tmp_path):
    runner = CliRunner()
    spec = tmp_path / "spec.yml"
    spec.write_text("repos: []\n")

    monkeypatch.setattr("mtng.cli.resolve_github_token", lambda token: "token")

    result = runner.invoke(
        mtng.cli.cli,
        ["generate", str(spec), "--since", "not-a-real-date-value"],
    )

    assert result.exit_code != 0
    assert "Could not parse date/time value for --since" in _plain(result.output)


def test_generate_accepts_preamble_with_pdf(monkeypatch, tmp_path):
    runner = CliRunner()
    spec = tmp_path / "spec.yml"
    spec.write_text("repos: []\n")
    preamble = tmp_path / "preamble.tex"
    preamble.write_text("\\usepackage{tikz}\n\\newcommand{\\x}{1}\n")
    output_pdf = tmp_path / "out.pdf"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    captured = {}

    class FakeTemporaryDirectory:
        def __enter__(self):
            return str(scratch)

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeProc:
        def __init__(self):
            self.stdout = []
            self.returncode = 0

        def wait(self):
            return 0

    async def fake_collect_repositories(*args, **kwargs):
        return {}

    def fake_popen(args, stdout, stderr, text):
        source = Path(args[-1])
        captured["source"] = source.read_text()
        (source.parent / "source.pdf").write_bytes(b"%PDF-1.7\n")
        return FakeProc()

    monkeypatch.setattr("mtng.cli.resolve_github_token", lambda token: "token")
    monkeypatch.setattr("mtng.cli.collect_repositories", fake_collect_repositories)
    monkeypatch.setattr("mtng.cli.find_latexmk", lambda: Path("/usr/bin/latexmk"))
    monkeypatch.setattr("mtng.cli.have_lualatex", lambda: False)
    monkeypatch.setattr("mtng.cli.TemporaryDirectory", FakeTemporaryDirectory)
    monkeypatch.setattr("mtng.cli.subprocess.Popen", fake_popen)

    result = runner.invoke(
        mtng.cli.cli,
        [
            "generate",
            str(spec),
            "--since",
            "2024-01-01",
            "--preamble",
            str(preamble),
            "--pdf",
            str(output_pdf),
        ],
    )

    assert result.exit_code == 0
    source = captured["source"]
    assert "\\documentclass[aspectratio=169,t,13pt,dvipsnames]{beamer}" in source
    assert "\\usepackage{tikz}" in source
    assert "\\begin{document}" in source
    assert "\\end{document}" in source
    assert output_pdf.exists()


def test_generate_rejects_full_document_as_preamble_with_pdf(monkeypatch, tmp_path):
    runner = CliRunner()
    spec = tmp_path / "spec.yml"
    spec.write_text("repos: []\n")
    preamble = tmp_path / "slides.tex"
    preamble.write_text(
        "\\documentclass{beamer}\n"
        "\\usepackage{tikz}\n"
        "\\newcommand{\\x}{1}\n"
        "\\begin{document}\n"
        "\\frame{Title}\n"
        "\\end{document}\n"
    )
    output_pdf = tmp_path / "out.pdf"
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    captured = {}

    class FakeTemporaryDirectory:
        def __enter__(self):
            return str(scratch)

        def __exit__(self, exc_type, exc, tb):
            return False

    class FakeProc:
        def __init__(self):
            self.stdout = []
            self.returncode = 0

        def wait(self):
            return 0

    async def fake_collect_repositories(*args, **kwargs):
        return {}

    def fake_popen(args, stdout, stderr, text):
        source = Path(args[-1])
        captured["source"] = source.read_text()
        (source.parent / "source.pdf").write_bytes(b"%PDF-1.7\n")
        return FakeProc()

    monkeypatch.setattr("mtng.cli.resolve_github_token", lambda token: "token")
    monkeypatch.setattr("mtng.cli.collect_repositories", fake_collect_repositories)
    monkeypatch.setattr("mtng.cli.find_latexmk", lambda: Path("/usr/bin/latexmk"))
    monkeypatch.setattr("mtng.cli.have_lualatex", lambda: False)
    monkeypatch.setattr("mtng.cli.TemporaryDirectory", FakeTemporaryDirectory)
    monkeypatch.setattr("mtng.cli.subprocess.Popen", fake_popen)

    result = runner.invoke(
        mtng.cli.cli,
        [
            "generate",
            str(spec),
            "--since",
            "2024-01-01",
            "--preamble",
            str(preamble),
            "--pdf",
            str(output_pdf),
        ],
    )

    assert result.exit_code != 0
    assert "--preamble appears to be a full LaTeX document" in _plain(result.output)


def test_preamble_command_emits_the_template_verbatim():
    """`mtng preamble` output is redirected into a .tex file, so it has to come
    out byte for byte: rich would eat '[...]' as markup and wrap long lines."""
    runner = CliRunner()

    result = runner.invoke(mtng.cli.cli, ["preamble"])

    assert result.exit_code == 0
    source = mtng.cli.env.loader.get_source(mtng.cli.env, "preamble.tex")[0]
    assert result.stdout.strip() == source.strip()


def test_schema_command_emits_valid_json():
    runner = CliRunner()

    result = runner.invoke(mtng.cli.cli, ["schema"])

    assert result.exit_code == 0
    assert json.loads(result.stdout)["title"] == "Spec"
