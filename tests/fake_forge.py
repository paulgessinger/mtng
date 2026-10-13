from datetime import datetime

from mtng.forge.base import FetchRequirements
from mtng.models import Issue, PullRequest


class FakeForge:
    """In-memory Forge for tests.

    ``open_issues`` mirrors how collect_repositories uses it: a call with a
    creation window gets ``recent_issues``, any other call gets the list for
    the first of its ``with_labels`` found in ``labelled`` (stale, needs
    discussion, ...). Every call is recorded in ``calls`` by method name.
    """

    cache_id = "fake"

    def __init__(
        self,
        merged_prs: list[PullRequest] = [],
        open_prs: list[PullRequest] = [],
        recent_issues: list[Issue] = [],
        labelled: dict[str, list[Issue]] = {},
        release: tuple[str, list[PullRequest]] | None = None,
    ):
        self._merged_prs = list(merged_prs)
        self._open_prs = list(open_prs)
        self._recent_issues = list(recent_issues)
        self._labelled = dict(labelled)
        self._release = release
        self.calls: list[str] = []

    async def merged_prs(
        self,
        repo_name: str,
        start: datetime,
        end: datetime,
        with_labels: list[str] = [],
        without_labels: list[str] = [],
    ) -> list[PullRequest]:
        self.calls.append("merged_prs")
        return list(self._merged_prs)

    async def release_prs(
        self,
        repo_name: str,
        release_ref: str,
        with_labels: list[str] = [],
        without_labels: list[str] = [],
    ) -> tuple[str, list[PullRequest]]:
        self.calls.append("release_prs")
        assert self._release is not None, "FakeForge has no release configured"
        tag, prs = self._release
        return tag, list(prs)

    async def open_prs(
        self,
        repo_name: str,
        with_labels: list[str] = [],
        without_labels: list[str] = [],
        requirements: FetchRequirements = FetchRequirements(),
    ) -> list[PullRequest]:
        self.calls.append("open_prs")
        return list(self._open_prs)

    async def open_issues(
        self,
        repo_name: str,
        with_labels: list[str] = [],
        without_labels: list[str] = [],
        start: datetime | None = None,
        end: datetime | None = None,
        include_prs: bool = False,
    ) -> list[Issue]:
        self.calls.append("open_issues")
        if start is not None or end is not None:
            return list(self._recent_issues)
        for label in with_labels:
            if label in self._labelled:
                return list(self._labelled[label])
        return []
