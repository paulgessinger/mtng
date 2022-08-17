from typing import List, Optional
import pydantic
from pydantic import validator, root_validator


class BaseModel(pydantic.BaseModel):
    class Config:
        extra = "forbid"


class Repository(BaseModel):
    name: str
    wip_label: Optional[str] = None
    show_wip: bool = False
    filter_labels: List[str] = pydantic.Field(default_factory=list)
    stale_label: Optional[str] = None
    do_open_prs: bool = True
    do_merged_prs: bool = True
    do_stale: bool = False
    do_recent_issues: bool = False

    @root_validator
    def stale_label_set(cls, values):
        if values["do_stale"] and values["stale_label"] is None:
            raise ValueError("Must set stale label to do stale list")
        return values


class Spec(BaseModel):
    repos: List[Repository]
