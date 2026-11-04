"""Versioned, credential-free collection records for offline report rendering."""

from dataclasses import asdict
from datetime import datetime
import inspect
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from mtng.forge.base import FetchRequirements, Forge
from mtng.models import Issue, PullRequest
from mtng.spec import Spec

METHODS = {"merged_prs", "open_prs", "open_issues", "release_prs"}


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RecordedIssue(Model):
    kind: Literal["issue"] = "issue"
    value: Issue


class RecordedPR(Model):
    kind: Literal["pull_request"] = "pull_request"
    value: PullRequest


class QueryResult(Model):
    method: Literal["merged_prs", "open_prs", "open_issues", "release_prs"]
    query: dict
    items: list[Annotated[RecordedIssue | RecordedPR, Field(discriminator="kind")]]
    release_tag: str | None = None


class BackendData(Model):
    forge: Literal["github", "gitlab"]
    host: str
    results: list[QueryResult] = Field(default_factory=list)


class Contribution(Model):
    title: str
    speakers: list[str]
    start_date: datetime
    url: str


class ReportData(Model):
    version: Literal[1] = 1
    spec: Spec
    since: AwareDatetime
    now: AwareDatetime
    release: str | None = None
    contributions: list[Contribution] = Field(default_factory=list)
    events: dict[str, list[Contribution]] = Field(default_factory=dict)
    summaries: dict[str, str] = Field(default_factory=dict)
    backends: list[BackendData]

    def forges(self):
        return {(data.forge, data.host): ReplayForge(data) for data in self.backends}


def query_args(method, args, kwargs):
    bound = inspect.signature(getattr(Forge, method)).bind(None, *args, **kwargs)
    bound.apply_defaults()
    query = dict(bound.arguments)
    query.pop("self")
    for name, value in query.items():
        if isinstance(value, datetime):
            query[name] = value.isoformat()
        elif isinstance(value, FetchRequirements):
            query[name] = asdict(value)
    return query


class RecordingForge:
    def __init__(self, backend, forge, host):
        self.backend = backend
        self.data = BackendData(forge=forge, host=host)

    def __getattr__(self, method):
        if method not in METHODS:
            raise AttributeError(method)

        async def record(*args, **kwargs):
            result = await getattr(self.backend, method)(*args, **kwargs)
            tag, items = result if method == "release_prs" else (None, result)
            # Copy before collection adds presentation fields to the models.
            records = [
                (
                    RecordedPR(value=item.model_copy(deep=True))
                    if isinstance(item, PullRequest)
                    else RecordedIssue(value=item.model_copy(deep=True))
                )
                for item in items
            ]
            self.data.results.append(
                QueryResult(
                    method=method,
                    query=query_args(method, args, kwargs),
                    items=records,
                    release_tag=tag,
                )
            )
            return result

        return record


class ReplayForge:
    def __init__(self, data):
        self.data = data

    def __getattr__(self, method):
        if method not in METHODS:
            raise AttributeError(method)

        async def replay(*args, **kwargs):
            query = query_args(method, args, kwargs)
            for result in self.data.results:
                if result.method != method:
                    continue
                saved, requested = dict(result.query), dict(query)
                available = saved.pop("requirements", {})
                needed = requested.pop("requirements", {})
                if saved != requested or any(
                    value and not available.get(key) for key, value in needed.items()
                ):
                    continue
                items = [item.value.model_copy(deep=True) for item in result.items]
                return (result.release_tag, items) if method == "release_prs" else items
            raise ValueError(
                f"Saved data does not contain {method} for {query['repo_name']} with these filters/enrichment. Collect a new snapshot."
            )

        return replay
