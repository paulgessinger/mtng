"""Shared, nested progress display; status output always goes to stderr."""

from contextlib import contextmanager
from contextvars import ContextVar

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    ProgressColumn,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)
from rich.text import Text

console = Console(stderr=True)
_active: ContextVar[Progress | None] = ContextVar("mtng_progress", default=None)


class CountsColumn(ProgressColumn):
    def render(self, task):
        unit = task.fields.get("unit", "items")
        count = str(int(task.completed))
        if task.total is not None:
            count += f"/{int(task.total)}"
        return Text(f"{count} {unit}")


class Step:
    def __init__(self, progress, task_id):
        self.progress = progress
        self.task_id = task_id

    def advance(self, count=1):
        self.progress.advance(self.task_id, count)


@contextmanager
def progress_step(description, total=None, unit="items"):
    progress = _active.get()
    owner = progress is None
    token = None
    if owner:
        progress = Progress(
            SpinnerColumn(),
            TextColumn("{task.description}", markup=False),
            BarColumn(),
            CountsColumn(),
            TimeElapsedColumn(),
            TimeRemainingColumn(),
            console=console,
            disable=not console.is_terminal or console.is_dumb_terminal,
            transient=True,
        )
        token = _active.set(progress)
        progress.start()
    task_id = progress.add_task(description, total=total, unit=unit)
    if progress.disable:
        console.print(description, markup=False, highlight=False)
    failed = False
    try:
        yield Step(progress, task_id)
    except BaseException:
        failed = True
        raise
    finally:
        task = next(task for task in progress.tasks if task.id == task_id)
        completed = int(task.completed)
        progress.remove_task(task_id)
        if owner:
            progress.stop()
            _active.reset(token)
        if owner or progress.disable:
            count = f"{completed}/{int(total)}" if total is not None else str(completed)
            state = "Stopped" if failed else "Done"
            console.print(
                f"{state}: {description} ({count} {unit})",
                markup=False,
                highlight=False,
            )


def progress_items(items, description, unit="items", item_label=None):
    with progress_step(description, total=len(items), unit=unit) as step:
        for item in items:
            if item_label is not None:
                step.progress.update(
                    step.task_id, description=f"{description}: {item_label(item)}"
                )
            yield item
            step.advance()


async def collect_items(iterator, convert, description):
    """Count search results without inventing a total before pagination finishes."""
    result = []
    with progress_step(description) as step:
        async for value in iterator:
            result.append(convert(value))
            step.advance()
    return result
