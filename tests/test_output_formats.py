"""Cross-format content, escaping, configuration, and offline CLI contracts."""

from datetime import datetime, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from pydantic import ValidationError
from typer.testing import CliRunner

from mtng.cli import cli, output_report, validate_output
from mtng.collect import collect_repositories
from mtng.document import ItemList, prepare_report
from mtng.generate import generate_latex
from mtng.models import PullRequest, User
from mtng.report_data import RecordingForge, ReportData
from mtng.renderers import RENDERERS
from mtng.spec import OutputFormat, Repository, Spec
from fake_forge import FakeForge

START = datetime(2026, 10, 1, tzinfo=timezone.utc)
END = datetime(2026, 11, 1, tzinfo=timezone.utc)


def request(number=1, **kwargs):
    fields = dict(
        labels=[],
        title=f"feat: Change {number}",
        user=User(login="alice", html_url="https://example.org/alice"),
        html_url=f"https://example.org/pr/{number}",
        url=f"https://example.org/api/{number}",
        number=number,
        created_at=START,
        updated_at=END,
        closed_at=END,
    )
    fields.update(kwargs)
    return PullRequest(**fields)


@pytest_asyncio.fixture
async def report():
    repo = Repository(name="org/project", do_reviewers=True, show_wip=True)
    spec = Spec(title="Weekly <report>", repos=[repo])
    data = await collect_repositories(
        [repo], START, END, FakeForge(merged_prs=[request()], open_prs=[request(2)])
    )
    return spec, data


@pytest.mark.asyncio
@pytest.mark.parametrize("format", ["html", "markdown", "text"])
async def test_same_report_content(report, format):
    spec, data = report
    doc = prepare_report(spec, data, START, END)
    output = RENDERERS[format](doc, getattr(spec.output, format))
    assert "Change 1" in output and "Change 2" in output
    assert output.index("Change 1") < output.index("Change 2")
    assert "alice" in output
    assert "2026" in output
    assert "\\reponame" not in output


@pytest.mark.asyncio
async def test_html_escaping_and_unsafe_links(report):
    spec, data = report
    item = data["org/project"]["open_prs"][0]
    item.pr_title_display = '<script>alert("x")</script>'
    item.html_url = "javascript:alert(1)"
    item.llm_summary = "<img src=x onerror=alert(1)>"
    output = RENDERERS["html"](prepare_report(spec, data, START, END), spec.output.html)
    assert "<script>" not in output and "<img" not in output
    assert "&lt;script&gt;" in output
    assert "javascript:" not in output
    spec.output.html.standalone = False
    output = RENDERERS["html"](prepare_report(spec, data, START, END), spec.output.html)
    assert "<article" in output and "<html" not in output and "<style" not in output


@pytest.mark.asyncio
async def test_markdown_reference_links_and_escaping(report):
    from mtng.renderers.text import markdown_escape

    assert markdown_escape("1. Item") == r"1\. Item"
    assert markdown_escape("&copy;") == r"\&copy;"
    spec, data = report
    item = data["org/project"]["open_prs"][0]
    item.pr_title_display = "[click](javascript:x) <b> *hello*"
    item.html_url = "https://example.org/a(b)?x=1&y=2"
    spec.output.markdown.links = "reference"
    output = RENDERERS["markdown"](
        prepare_report(spec, data, START, END), spec.output.markdown
    )
    assert r"\[click\]" in output and r"\<b\>" in output
    assert "[2]: <https://example.org/a%28b%29?x=1&y=2>" in output
    assert "<table" not in output


@pytest.mark.asyncio
async def test_text_wrapping_preserves_urls(report):
    spec, data = report
    spec.output.text.width = 30
    spec.output.text.unicode = False
    output = RENDERERS["text"](prepare_report(spec, data, START, END), spec.output.text)
    assert "https://example.org/pr/1" in output
    assert output.isascii()
    assert "](" not in output and "<html" not in output
    spec.output.text.links = "omit"
    output = RENDERERS["text"](prepare_report(spec, data, START, END), spec.output.text)
    assert "https://" not in output


@pytest.mark.asyncio
async def test_format_headings_and_hex_colors(report):
    spec, data = report
    spec.presentation.headings["open_prs"] = "Shared {repo}"
    spec.output.latex.headings["open_prs"] = "Slides {repo}"
    spec.output.html.headings["open_prs"] = "Web {repo}"
    spec.presentation.colors["feat"] = "#65A30D"
    spec.output.latex.colors["feat"] = "#4D7C0F"
    document = prepare_report(spec, data, START, END)
    html = RENDERERS["html"](document, spec.output.html)
    plain = RENDERERS["text"](document, spec.output.text)
    latex = generate_latex(spec, data, START, END, [], False)
    assert "Web org/project" in html
    assert "Shared org/project" in plain
    assert r"Slides \reponame{}" in latex
    assert r"\definecolor{mtngcolor4D7C0F}{HTML}{4D7C0F}" in latex
    assert "#65A30D" in html
    assert data["org/project"]["open_prs"][0].pr_category_color != "mtngcolor4D7C0F"


@pytest.mark.asyncio
async def test_document_breaking_repeat_and_draft_order():
    repo = Repository(name="org/project", repeat_breaking_changes=False, show_wip=True)
    data = await collect_repositories(
        [repo],
        START,
        END,
        FakeForge(
            merged_prs=[request(1, title="feat!: Break"), request(2)],
            open_prs=[request(3, draft=True), request(4)],
        ),
    )
    document = prepare_report(Spec(repos=[repo]), data, START, END)
    merged = [
        item.number
        for section in document.sections
        for block in section.blocks
        if isinstance(block, ItemList) and block.mode == "MERGED"
        for item in block.items
    ]
    opened = [
        item.number
        for section in document.sections
        for block in section.blocks
        if isinstance(block, ItemList) and block.mode == "OPEN"
        for item in block.items
    ]
    assert merged == [1, 2]
    assert opened == [4, 3]


@pytest.mark.parametrize(
    "config",
    [
        {"output": {"format": "docx"}},
        {"output": {"text": {"width": -1}}},
        {"output": {"markdown": {"statistics": "table"}}},
        {"output": {"html": {"layout": "slides"}}},
        {"presentation": {"colors": {"feat": "red"}}},
        {"output": {"latex": {"headings": {"typo": "title"}}}},
    ],
)
def test_invalid_configuration(config):
    with pytest.raises(ValidationError):
        Spec.model_validate(config)


def test_output_selection_and_conflicts(tmp_path):
    spec = Spec(output={"format": "html"})
    assert validate_output(spec, None, None) == OutputFormat.html
    assert validate_output(spec, OutputFormat.markdown, None) == OutputFormat.markdown
    assert (
        validate_output(spec, None, None, tex=tmp_path / "a.tex") == OutputFormat.latex
    )
    with pytest.raises(Exception, match="PDF output requires"):
        validate_output(spec, OutputFormat.pdf, None)
    with pytest.raises(Exception, match="require LaTeX"):
        validate_output(spec, OutputFormat.html, None, full_tex=True)


@pytest.mark.parametrize(
    "name, expected",
    [
        ("report.html", OutputFormat.html),
        ("report.HTM", OutputFormat.html),
        ("report.md", OutputFormat.markdown),
        ("report.txt", OutputFormat.text),
        ("report.pdf", OutputFormat.pdf),
        ("report.tex", OutputFormat.latex),
        ("report.out", OutputFormat.latex),
    ],
)
def test_output_format_from_extension(tmp_path, name, expected):
    assert validate_output(Spec(), None, tmp_path / name) == expected


def test_output_extension_does_not_override_explicit_format(tmp_path):
    output = tmp_path / "report.html"
    assert validate_output(Spec(), OutputFormat.text, output) == OutputFormat.text
    spec = Spec(output={"format": "markdown"})
    assert validate_output(spec, None, output) == OutputFormat.markdown
    spec = Spec(output={"format": "latex"})
    assert validate_output(spec, None, output) == OutputFormat.latex


@pytest.mark.asyncio
async def test_offline_cli_all_formats(tmp_path):
    repo = Repository(name="org/project")
    spec = Spec(repos=[repo], output={"format": "html"})
    recorder = RecordingForge(FakeForge(merged_prs=[request()]), *repo.forge_key)
    await collect_repositories([repo], START, END, recorder)
    path = tmp_path / "data.json"
    path.write_text(
        ReportData(
            spec=spec, since=START, now=END, backends=[recorder.data]
        ).model_dump_json()
    )
    # CLI uses its own event loop, so invoke it from a worker thread.
    import asyncio

    def invoke():
        runner = CliRunner()
        for format in ("html", "markdown", "text", "latex"):
            output = tmp_path / format
            result = runner.invoke(
                cli, ["render", str(path), "--format", format, "--output", str(output)]
            )
            assert result.exit_code == 0, result.output + repr(result.exception)
            assert output.is_file()
            assert "https://example.org/pr/1" in output.read_text()
        result = runner.invoke(cli, ["render", str(path)])
        assert result.exit_code == 0 and "<!doctype html>" in result.stdout

    await asyncio.to_thread(invoke)


def test_invalid_options_fail_before_collection(tmp_path):
    config = tmp_path / "report.toml"
    config.write_text('[[repos]]\nname = "org/project"\n')
    result = CliRunner().invoke(
        cli,
        [
            "generate",
            str(config),
            "--format",
            "html",
            "--pdf",
            str(tmp_path / "report.pdf"),
        ],
    )
    assert result.exit_code != 0
    assert "require LaTeX/PDF" in result.output


@pytest.mark.asyncio
async def test_shared_and_format_visibility(report):
    spec, data = report
    item = data["org/project"]["open_prs"][0]
    item.llm_summary = "A useful explanation"
    spec.presentation.show_summaries = False
    spec.presentation.metadata = []
    spec.output.html.show_summaries = True
    document = prepare_report(spec, data, START, END)
    html = RENDERERS["html"](document, spec.output.html)
    plain = RENDERERS["text"](document, spec.output.text)
    latex = generate_latex(spec, data, START, END, [], False)
    assert "A useful explanation" in html
    assert "A useful explanation" not in plain and "A useful explanation" not in latex
    assert "alice" not in html and "alice" not in plain
    assert "https://example.org/alice" not in latex


@pytest.mark.asyncio
async def test_explicit_sections_agenda_and_overrides():
    from mtng.sections import collect_sections

    spec = Spec.model_validate(
        {
            "presentation": {"colors": {"feat": "#112233"}},
            "repositories": {
                "project": {
                    "name": "org/project",
                    "presentation": {"colors": {"feat": "#223344"}},
                }
            },
            "sections": [
                {
                    "type": "indico",
                    "event": "https://example.org/event/1",
                    "title": "Agenda",
                },
                {
                    "type": "changes",
                    "state": "open",
                    "repositories": ["project"],
                    "presentation": {
                        "headings": {"open_prs": "To discuss"},
                        "colors": {"feat": "#334455"},
                    },
                },
            ],
        }
    )
    data = await collect_sections(
        spec, START, END, {("github", "github.com"): FakeForge(open_prs=[request()])}
    )
    events = {
        "https://example.org/event/1": [
            {
                "title": "Planning <next>",
                "url": "https://example.org/talk",
                "speakers": ["Alice"],
            }
        ]
    }
    document = prepare_report(spec, data, START, END, events=events)
    html = RENDERERS["html"](document, spec.output.html)
    assert html.index("Planning &lt;next&gt;") < html.index("Change 1")
    assert "To discuss" in html and "#334455" in html
    latex = generate_latex(spec, data, START, END, [], False, events)
    assert "To discuss" in latex
    assert r"\definecolor{mtngcolor334455}{HTML}{334455}" in latex


@pytest.mark.asyncio
async def test_review_followup_nested_groups_in_every_format():
    from mtng.document import Heading, ReportDocument, ReportSection
    from mtng.spec import Presentation

    block = ItemList(
        [request()],
        "OPEN",
        "Awaiting review",
        "alice",
        [ItemList([request(2)], "OPEN", "Re-review")],
        actor_url="https://example.org/alice",
    )
    section = ReportSection(
        "reviews",
        Heading("review_followup", {}, "Review follow-up"),
        Repository(name="org/project"),
        Presentation(),
        [block],
    )
    document = ReportDocument("Reviews", START, END, [section], {})
    spec = Spec()
    for format in ("html", "markdown", "text"):
        rendered = RENDERERS[format](document, getattr(spec.output, format))
        assert "Change 1" in rendered and "Change 2" in rendered
        assert "alice" in rendered and "review" in rendered


def test_pdf_output_routes_engine_and_destination(monkeypatch, tmp_path):
    from unittest.mock import Mock
    import mtng.cli as module

    spec = Spec(
        output={
            "pdf": {"engine": "pdflatex"},
            "latex": {"aspect_ratio": "4:3", "font_size": 11},
        }
    )
    monkeypatch.setattr(module, "find_latexmk", lambda: Path("latexmk"))
    commands = []

    def popen(args, **kwargs):
        commands.append(args)
        Path(args[-1]).with_suffix(".pdf").write_bytes(b"%PDF-test")
        return Mock(stdout=[], returncode=0)

    monkeypatch.setattr(module.subprocess, "Popen", popen)
    path = tmp_path / "report.pdf"
    output_report(spec, {}, START, END, [], output_format=OutputFormat.pdf, output=path)
    assert path.read_bytes() == b"%PDF-test"
    assert "-pdflatex=pdflatex" in commands[0]
