import asyncio
from io import StringIO

import pytest
from rich.console import Console

import mtng.progress as progress
from mtng.forge.base import bounded_gather


@pytest.fixture
def output(monkeypatch):
    stream = StringIO()
    monkeypatch.setattr(
        progress, "console", Console(file=stream, force_terminal=False, width=120)
    )
    return stream


@pytest.mark.asyncio
async def test_batch_reports_completions_and_preserves_order(output):
    async def fetch(value):
        await asyncio.sleep(0)
        return value * 2

    assert await bounded_gather(
        [1, 2, 3], fetch, concurrency=2, description="PR details"
    ) == [2, 4, 6]
    assert "Done: PR details (3/3 items)" in output.getvalue()
    assert progress._active.get() is None


@pytest.mark.asyncio
async def test_failure_cancels_workers_before_progress_closes(output):
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fetch(value):
        if value == 0:
            await started.wait()
            raise ValueError("failed request")
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    with pytest.raises(ValueError, match="failed request"):
        await bounded_gather([0, 1], fetch, description="PR reviews")
    assert cancelled.is_set()
    assert "Stopped: PR reviews (0/2 items)" in output.getvalue()
    assert "Done: PR reviews" not in output.getvalue()
    assert progress._active.get() is None


@pytest.mark.asyncio
async def test_unknown_total_counts_search_items(output):
    async def results():
        for value in range(3):
            yield value

    assert await progress.collect_items(results(), str, "Finding issues") == [
        "0",
        "1",
        "2",
    ]
    assert "Done: Finding issues (3 items)" in output.getvalue()
    assert "/3" not in output.getvalue()


def test_nested_tasks_share_display_and_restore_context(output):
    with progress.progress_step("Report", total=1, unit="sections") as outer:
        display = progress._active.get()
        with progress.progress_step("Reviews", total=2) as inner:
            assert progress._active.get() is display
            assert len(display.tasks) == 2
            inner.advance(2)
        assert len(display.tasks) == 1
        outer.advance()
    assert progress._active.get() is None
    assert "Done: Report (1/1 sections)" in output.getvalue()


def test_empty_and_failed_repository_iteration(output):
    assert list(progress.progress_items([], "Repositories", unit="repositories")) == []
    assert "Done: Repositories (0/0 repositories)" in output.getvalue()
    iterator = progress.progress_items([1, 2], "Repositories", unit="repositories")
    assert next(iterator) == 1
    iterator.close()
    assert progress._active.get() is None
    assert "Stopped: Repositories (0/2 repositories)" in output.getvalue()


def test_terminal_uses_shared_live_progress(monkeypatch):
    monkeypatch.setenv("TERM", "xterm-256color")
    stream = StringIO()
    monkeypatch.setattr(
        progress, "console", Console(file=stream, force_terminal=True, width=120)
    )
    with progress.progress_step("Report", total=2, unit="sections") as step:
        step.advance()
        step.progress.refresh()
        assert "1/2 sections" in stream.getvalue()
        step.advance()
    assert "Done: Report (2/2 sections)" in stream.getvalue()
