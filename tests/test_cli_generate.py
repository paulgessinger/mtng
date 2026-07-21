import re
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
