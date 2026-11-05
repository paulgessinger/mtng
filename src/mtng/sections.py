"""Collect ordered report sections while retaining the legacy repository collector."""

from copy import deepcopy
from mtng.progress import progress_items

from mtng.collect import collect_repositories, group_prs_by_category
from mtng.spec import (
    ChangesSection,
    DiscussionSection,
    IndicoSection,
    IssuesSection,
    Repository,
    ReviewFollowupSection,
    StaleSection,
    StatisticsSection,
)


def filter_drafts(items, drafts):
    """Apply a section's ``drafts`` selection; native draft state and the
    repository's WIP label both count."""

    if drafts == "include":
        return items
    return [item for item in items if item.is_wip == (drafts == "only")]


async def collect_sections(spec, since, now, forges, release=None):
    data = {}
    for index, section in progress_items(
        [
            (i, s)
            for i, s in enumerate(spec.sections)
            if not isinstance(s, IndicoSection)
        ],
        "Collecting repository sections",
        unit="sections",
        item_label=lambda item: item[1].title or item[1].type,
    ):
        for alias in section.repositories:
            source = spec.repositories[alias]
            values = source.model_dump()
            values.update(
                do_open_prs=False,
                do_review_followup=False,
                do_merged_prs=False,
                do_recent_issues=False,
                stale_label=None,
                needs_discussion_label=None,
                filter_labels=[],
                include_labels=section.labels.include,
                exclude_labels=section.labels.exclude,
            )
            section_release = None
            if isinstance(section, ChangesSection):
                values.update(
                    do_open_prs=section.state == "open",
                    do_merged_prs=section.state == "merged",
                    show_wip=True,
                    group_prs_by_category=section.group_by == "category",
                )
                if section.state == "merged":
                    section_release = section.release or release
            elif isinstance(section, ReviewFollowupSection):
                values["do_review_followup"] = True
                if section.assignment_source is not None:
                    values["review_assignment_source"] = section.assignment_source
                if section.review_completion is not None:
                    values["review_completion"] = section.review_completion
            elif isinstance(section, StatisticsSection):
                values["do_merged_prs"] = True
                section_release = section.release or release
            elif isinstance(section, IssuesSection):
                values["do_recent_issues"] = True
            elif isinstance(section, StaleSection):
                values["stale_label"] = section.label
            elif isinstance(section, DiscussionSection):
                values["needs_discussion_label"] = section.label
            if section.title is not None:
                values["frame_titles"] = section.title
            repo = Repository.model_validate(values)
            result = await collect_repositories(
                [repo], since, now, forges, release=section_release, where=section.where
            )
            block = deepcopy(next(iter(result.values())))
            if isinstance(section, ChangesSection) and section.state == "open":
                block["open_prs"] = filter_drafts(
                    block["open_prs"], section.drafts or "exclude"
                )
                block["open_prs_by_category"] = group_prs_by_category(block["open_prs"])
            elif isinstance(section, StaleSection):
                block["stale"] = filter_drafts(block["stale"], section.drafts)
            elif isinstance(section, DiscussionSection):
                block["needs_discussion"] = filter_drafts(
                    block["needs_discussion"], section.drafts
                )
            block["section_index"] = index
            block["llm_summary"] = section.llm_summary
            data[f"{index}:{alias}"] = block
    return data
