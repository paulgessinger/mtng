# Repository Guidelines

## Project Structure & Module Organization

`mtng` generates LaTeX meeting presentations from GitHub activity and optional Indico agendas. Python code lives in `src/mtng/`: `cli.py` defines the Typer interface, `collect.py` retrieves data, `spec.py` defines Pydantic configuration models, `generate.py` renders output, and `stats.py` computes statistics. `action.py` supports the root `action.yml` GitHub Action. Backend adapters and HTTP caching live in `forge/`; `report_data.py` supports offline replay. Jinja2/LaTeX templates live in `src/mtng/template/`.

Tests live in `tests/`, with JSON fixtures and the LaTeX snapshot in `tests/ref/`. `tests/acts_spec.yml` is an example configuration; `screen.png` illustrates output. CI workflows are in `.github/workflows/`.

## Build, Test, and Development Commands

Use Python 3.12 or newer and uv:

- `uv sync --group dev`: install project and development dependencies.
- `uv run mtng --help`: inspect the local CLI.
- `uv run --group dev pytest -m "not integration" -n auto`: run the local test suite with CI's parallel settings.
- `uv run --group dev pytest -m integration -n 1`: run external-service tests with a valid `GH_TOKEN`.
- `uv run --group dev nox`: test across Python 3.12, 3.13, and 3.14.
- `uv run --group dev black .`: format Python; use `black --check .` for verification.
- `uv build` (or `just build`): build distribution artifacts.

PDF generation and compilation tests require a suitable TeX Live installation and `latexmk`.

## Coding Style & Naming Conventions

Use four-space indentation and Black's default formatting. Follow existing `snake_case` function/module names, `PascalCase` classes, and `UPPER_SNAKE_CASE` constants. Prefer modern annotations such as `list[str]` and `str | None`. Keep configuration validation in `spec.py` and presentation markup in templates.

## Testing Guidelines

Use pytest, pytest-asyncio for asynchronous tests, and Typer's `CliRunner` for CLI behavior. Name files `test_*.py` and functions `test_*`. Mock external calls in ordinary tests; mark live-service tests `integration`. No numerical coverage threshold is configured.

For intentional rendering changes, regenerate the snapshot with `UPDATE_SNAPSHOTS=1 uv run pytest tests/test_generate.py::test_generate` and review the resulting diff.

## Commit & Pull Request Guidelines

Recent commits favor Conventional Commits, such as `feat(slides): ...` and `fix(tests): ...`; older history also uses imperative summaries. Prefer the recent convention. PRs should explain the behavior change, link relevant issues, and report validation. Include rendered examples for slide changes and update `README.md` when CLI options or configuration change.

## Credentials

Use `GH_TOKEN` or `mtng auth login` for authentication. Never commit tokens or credential-bearing configuration.
