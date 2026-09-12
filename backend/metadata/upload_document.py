"""Build the one metadata model rendered into the upload and CivitAI ``meta``."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from . import compose, infotext, resources, upload_resources

RESOURCE_FIELDS = upload_resources.RESOURCE_FIELDS
LIVEBOUND_KEY = "livebound"

_KEY_MAP = {
    "Seed": "seed",
    "CFG scale": "cfgScale",
    "Sampler": "sampler",
    "Steps": "steps",
    "Clip skip": "clipSkip",
}


@dataclass(frozen=True)
class Document:
    """Effective upload metadata before its A1111 and CivitAI renderings."""

    prompt: str
    negative_prompt: str | None
    parameters: tuple[infotext.ParameterEntry, ...]
    resources: tuple[dict[str, Any], ...]
    source_resources: tuple[dict[str, Any], ...]
    civitai_resources: tuple[dict[str, Any], ...]
    source_generator: str | None


def build(
    text: str,
    resource_rows: list[dict[str, Any]],
    retained_rows: list[dict[str, Any]] | None = None,
    source_generator: str | None = None,
    parsed: dict[str, Any] | None = None,
) -> Document:
    """Build one source for the A1111 block, CivitAI block, and ``meta``."""
    retained = resource_rows if retained_rows is None else retained_rows
    document = infotext.parse(text) if parsed is None else parsed
    fields = document.get("fields") or {}
    canonical_rows, resource_values, credits = upload_resources.project(
        fields, resource_rows, retained
    )

    _, parameter_line = compose._split(text.rstrip("\n"))
    source_parameters, _ = infotext.parse_parameter_line(parameter_line)
    if not source_parameters and fields:
        source_parameters = [
            infotext.ParameterEntry(
                key=key,
                value=fields[key],
                raw=compose.render(fields[key]),
                prefix=", ",
                source=None,
            )
            for key in document.get("field_order") or fields
            if key in fields
        ]
    parameters: list[infotext.ParameterEntry] = []
    rendered_resource_fields: set[str] = set()
    for parameter in source_parameters:
        key = parameter.key
        if key in ("Civitai resources", LIVEBOUND_KEY):
            continue
        if key not in RESOURCE_FIELDS:
            parameters.append(parameter)
            continue
        if key in rendered_resource_fields or key not in resource_values:
            continue
        parameters.append(
            infotext.ParameterEntry(
                key=key,
                value=resource_values[key],
                raw=_render_resource_value(key, resource_values[key]),
                prefix=parameter.prefix,
                source=f"{key}: {_render_resource_value(key, resource_values[key])}",
            )
        )
        rendered_resource_fields.add(key)

    if credits:
        parameters.append(
            infotext.ParameterEntry(
                key="Civitai resources",
                value=list(credits),
                raw=compose.render(list(credits)),
                prefix=", ",
                source=None,
            )
        )

    return Document(
        prompt=str(document.get("prompt") or ""),
        negative_prompt=document.get("negative_prompt"),
        parameters=tuple(parameters),
        resources=tuple(canonical_rows),
        source_resources=tuple(resource_rows),
        civitai_resources=credits,
        source_generator=source_generator,
    )


def render_infotext(document: Document) -> str:
    """Render the familiar A1111 structure followed by the CivitAI field."""
    parameters = list(document.parameters)
    if document.source_generator is not None:
        parameters.append(
            infotext.ParameterEntry(
                key=LIVEBOUND_KEY,
                value={"generator": document.source_generator},
                raw=compose.render({"generator": document.source_generator}),
                prefix=", ",
                source=None,
            )
        )
    return compose.render_infotext(document.prompt, document.negative_prompt, parameters)


def _render_resource_value(key: str, value: Any) -> str:
    """Keep A1111's map-valued resource fields in their JSON string form."""
    if key in {"Lora hashes", "Lora weights", "TI hashes"} and isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    return compose.render(value)


def render_meta(document: Document) -> dict[str, Any] | None:
    """Render CivitAI's loose ``meta`` object without parsing another rendering."""
    rendered: dict[str, Any] = {}
    if document.prompt:
        rendered["prompt"] = document.prompt
    if document.negative_prompt is not None:
        rendered["negativePrompt"] = document.negative_prompt

    for parameter in document.parameters:
        key, value, raw = parameter.key, parameter.value, parameter.raw
        if key in RESOURCE_FIELDS or key == LIVEBOUND_KEY:
            continue
        if key == "Size":
            dimensions = _dimensions(raw)
            if dimensions is not None:
                rendered["width"], rendered["height"] = dimensions
            else:
                rendered[key] = _civitai_value(key, raw, value)
            continue
        target = _KEY_MAP.get(key, key)
        if key == "Model":
            rendered[target] = str(value)
        else:
            rendered[target] = value if key in _KEY_MAP else _civitai_value(key, raw, value)

    identified = [dict(entry) for entry in document.civitai_resources]
    if identified and not _has_external_model(rendered) and "Version" not in rendered:
        checkpoint_name = next(
            (
                str(row.get("name_in_prompt") or "").strip()
                for row in document.resources
                if row.get("resource_type") == resources.CHECKPOINT
                and str(row.get("name_in_prompt") or "").strip() != "model"
            ),
            "",
        )
        # Credits without an ordinary external `Model` are what CivitAI reads as
        # "generated on our site" (`CVT-15`). Where the rows actually name a
        # checkpoint, say so and the question does not arise.
        #
        # Where they do not, the credits still go. An image CivitAI generated
        # carries no checkpoint file name at all - only a model version id - and
        # withholding its credits would throw away the one identity it has to
        # avoid a claim that is true of it. The maintainer decided this on
        # 2026-09-06, and `MET-11` already accepts the classification as the
        # consequence of having no `Model`. Nothing is invented either way: a
        # version id is not a name and is never written as one.
        if checkpoint_name and not checkpoint_name.startswith("urn:air:"):
            rendered["Model"] = checkpoint_name

    if identified:
        rendered["civitaiResources"] = identified
    return rendered or None


def _has_external_model(document: dict[str, Any]) -> bool:
    model = document.get("Model")
    return isinstance(model, str) and bool(model) and not model.startswith("urn:air:")


def _dimensions(raw: str) -> tuple[int, int] | None:
    value = raw.strip().strip('"')
    parts = value.lower().split("x", 1)
    if len(parts) != 2:
        return None
    try:
        width, height = (int(part.strip()) for part in parts)
    except ValueError:
        return None
    return (width, height) if width > 0 and height > 0 else None


def _civitai_value(key: str, raw: str, decoded: Any) -> Any:
    """Keep useful Automatic shapes without turning prompt prose into details."""
    if key.casefold().endswith("prompt") and isinstance(decoded, str):
        return decoded
    if not raw.startswith('"'):
        return raw if not isinstance(decoded, (dict, list)) else decoded
    try:
        nested = json.loads(raw)
    except json.JSONDecodeError:
        return decoded
    if not isinstance(nested, str):
        return nested
    parsed = _nested_details(nested)
    return parsed or nested


def _nested_details(text: str) -> dict[str, str]:
    """The shape Automatic produces for generator-quoted detail strings."""
    result: dict[str, str] = {}
    current_key = ""
    current_value = ""
    inside_quotes = False
    inside_date = False
    for char in text:
        if char == '"':
            if inside_quotes:
                result[current_key] = current_value.strip()
                current_key = ""
            inside_quotes = not inside_quotes
        elif char == ":" and not inside_quotes and not inside_date:
            if len(current_value) == 14 and current_value[11:12] == "T":
                inside_date = True
            else:
                current_key = current_value.strip()
                current_value = ""
        elif char == "," and not inside_quotes:
            if inside_date:
                inside_date = False
            if current_key:
                result[current_key] = current_value.strip()
            current_key = ""
            current_value = ""
        else:
            current_value += char
    if current_key:
        result[current_key] = current_value.strip()
    return result
