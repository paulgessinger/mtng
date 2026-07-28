"""Turn GitHub Action inputs into an ``mtng`` spec file.

The composite action in ``action.yml`` exposes every ``Spec``/``Repository``
field as an action input, so a workflow can either point at a checked-in config
file or configure a single repository inline. Inputs reach this module as
``MTNG_INPUT_<FIELD>`` environment variables; the field list and the coercion of
each value are derived from the pydantic models, so a new spec field only needs
to be added to ``action.yml``.
"""

import os
import sys
import types
import typing
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Union

import pydantic

from mtng.spec import Repository, Spec

ENV_PREFIX = "MTNG_INPUT_"

CONFIG_INPUT = "config"
REPOSITORY_INPUT = "repository"

# The repository name comes from the dedicated `repository` input, which falls
# back to the repository the action runs in.
REPOSITORY_FIELDS = [name for name in Repository.model_fields if name != "name"]
SPEC_FIELDS = [name for name in Spec.model_fields if name != "repos"]

TRUE_VALUES = {"true", "1", "yes", "on"}
FALSE_VALUES = {"false", "0", "no", "off"}


class ActionInputError(Exception):
    pass


def env_name(input_name: str) -> str:
    return ENV_PREFIX + input_name.replace("-", "_").upper()


def get_input(inputs: Mapping[str, str], input_name: str) -> str:
    return (inputs.get(env_name(input_name)) or "").strip()


def split_entries(value: str) -> List[str]:
    """Split a multi-value input. Entries are separated by newlines, commas, or
    both, which are the two conventions actions typically accept."""
    entries = []
    for line in value.splitlines():
        for entry in line.split(","):
            entry = entry.strip()
            if entry != "":
                entries.append(entry)
    return entries


def parse_bool(value: str, input_name: str) -> bool:
    normalized = value.strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ActionInputError(
        f"Input '{input_name}' expects a boolean value (true/false), got {value!r}."
    )


def parse_mapping(value: str, input_name: str) -> Dict[str, str]:
    mapping = {}
    for entry in split_entries(value):
        if "=" in entry:
            key, _, item = entry.partition("=")
        elif ":" in entry:
            key, _, item = entry.partition(":")
        else:
            raise ActionInputError(
                f"Input '{input_name}' expects 'key=value' entries, got {entry!r}."
            )
        key = key.strip()
        item = item.strip()
        if key == "":
            raise ActionInputError(
                f"Input '{input_name}' has an entry with an empty key: {entry!r}."
            )
        mapping[key] = item
    return mapping


def unwrap_optional(annotation: Any) -> Any:
    origin = typing.get_origin(annotation)
    if origin is Union or origin is types.UnionType:
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return args[0]
    return annotation


def coerce_value(annotation: Any, value: str, input_name: str) -> Any:
    annotation = unwrap_optional(annotation)
    origin = typing.get_origin(annotation)

    if annotation is bool:
        return parse_bool(value, input_name)
    if origin is list:
        return split_entries(value)
    if origin is dict:
        return parse_mapping(value, input_name)
    return value


def collect_fields(
    inputs: Mapping[str, str], model: type[pydantic.BaseModel], fields: List[str]
) -> Dict[str, Any]:
    """Read the subset of `fields` that were actually passed. Empty inputs are
    dropped so the model defaults stay in charge."""
    data: Dict[str, Any] = {}
    for field in fields:
        value = get_input(inputs, field)
        if value == "":
            continue
        data[field] = coerce_value(
            model.model_fields[field].annotation, value, field.replace("_", "-")
        )
    return data


def provided_spec_inputs(inputs: Mapping[str, str]) -> List[str]:
    """Spec-describing inputs that were set, in action (kebab-case) spelling."""
    provided = []
    for field in [REPOSITORY_INPUT] + SPEC_FIELDS + REPOSITORY_FIELDS:
        if get_input(inputs, field) != "":
            provided.append(field.replace("_", "-"))
    return provided


def build_spec(inputs: Mapping[str, str]) -> Spec:
    repository = (
        get_input(inputs, REPOSITORY_INPUT)
        or (inputs.get("GITHUB_REPOSITORY") or "").strip()
    )
    if repository == "":
        raise ActionInputError(
            "No repository to summarize: set the 'repository' input, or run the "
            "action inside a GitHub repository."
        )

    repo = {"name": repository}
    repo.update(collect_fields(inputs, Repository, REPOSITORY_FIELDS))

    data: Dict[str, Any] = collect_fields(inputs, Spec, SPEC_FIELDS)
    data["repos"] = [repo]

    try:
        return Spec.model_validate(data)
    except pydantic.ValidationError as e:
        raise ActionInputError(f"Invalid action inputs: {e}") from e


def resolve_config(inputs: Mapping[str, str], generated: Path) -> Path:
    """Return the spec file `mtng generate` should consume, writing one from the
    inline inputs unless a config file was given."""
    config = get_input(inputs, CONFIG_INPUT)

    if config != "":
        conflicting = provided_spec_inputs(inputs)
        if conflicting:
            raise ActionInputError(
                f"The 'config' input cannot be combined with {', '.join(conflicting)}. "
                "Either point at a config file or configure the repository through inputs."
            )
        path = Path(config)
        if not path.is_file():
            raise ActionInputError(f"Config file '{config}' does not exist.")
        return path

    spec = build_spec(inputs)
    generated.parent.mkdir(parents=True, exist_ok=True)
    generated.write_text(spec.model_dump_json(indent=2))
    return generated


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("usage: python -m mtng.action <generated-spec-path>", file=sys.stderr)
        return 2

    generated = Path(argv[0])
    try:
        config = resolve_config(os.environ, generated)
    except ActionInputError as e:
        print(f"::error::{e}", file=sys.stderr)
        return 2

    if config == generated:
        print("Generated spec from action inputs:", file=sys.stderr)
        print(config.read_text(), file=sys.stderr)

    print(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
