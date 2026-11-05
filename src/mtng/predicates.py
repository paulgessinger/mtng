"""Short-circuit section predicates, with lazy changed-path retrieval."""

from fnmatch import fnmatchcase
from functools import lru_cache

from mtng.forge.base import bounded_gather


def path_matches(path: str, pattern: str) -> bool:
    """Root-relative glob: * and ? stay in a segment, ** spans segments."""
    parts, patterns = path.split("/"), pattern.split("/")

    @lru_cache(None)
    def match(i, j):
        if j == len(patterns):
            return i == len(parts)
        if patterns[j] == "**":
            return match(i, j + 1) or (i < len(parts) and match(i + 1, j))
        return (
            i < len(parts)
            and fnmatchcase(parts[i], patterns[j])
            and match(i + 1, j + 1)
        )

    return match(0, 0)


async def matches(predicate, item, paths):
    if predicate.all is not None:
        for child in predicate.all:
            if not await matches(child, item, paths):
                return False
        return True
    if predicate.any is not None:
        for child in predicate.any:
            if await matches(child, item, paths):
                return True
        return False
    if predicate.not_ is not None:
        return not await matches(predicate.not_, item, paths)
    labels = {label.name for label in item.labels}
    if predicate.labels_all is not None:
        return set(predicate.labels_all) <= labels
    if predicate.labels_any is not None:
        return bool(set(predicate.labels_any) & labels)
    if not item.is_pr:
        return False
    return any(
        path_matches(path, pattern)
        for path in await paths()
        for pattern in predicate.paths_any
    )


async def filter_items(items, predicate, forge, repo_name):
    if predicate is None:
        return items

    async def keep(item):
        cached = None

        async def paths():
            nonlocal cached
            if cached is None:
                cached = await forge.changed_paths(
                    repo_name,
                    item.number,
                    merged=getattr(item, "merged_at", None) is not None,
                )
            return cached

        return await matches(predicate, item, paths)

    selected = await bounded_gather(items, keep, description="Filtering items")
    return [item for item, keep_item in zip(items, selected) if keep_item]
