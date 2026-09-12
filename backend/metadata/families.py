"""What every generator family's adapter needs and none of them should repeat.

Each family writes its own names for the same handful of facts. They are mapped
onto the names this application already carries - the ones an A1111 infotext
produces, which the metadata settings list and the field inventory collects - so
that one fact does not arrive under two spellings depending on who wrote the
file.
"""

from __future__ import annotations

import json
from typing import Any

#: Fields whose value is a whole number wherever it comes from.
INT_FIELDS = frozenset({"Steps", "Seed", "Clip skip"})

#: Fields whose value is fractional wherever it comes from.
FLOAT_FIELDS = frozenset({"CFG scale", "Denoising strength"})

#: The generator names written into a replacement block's ``livebound`` value.
ORIGIN_LABELS = {
    "comfyui": "ComfyUI",
    "fooocus": "Fooocus",
    "fooocusplus": "FooocusPlus",
    "hf-space": "Hugging Face Space",
    "invokeai": "InvokeAI",
    "novelai": "NovelAI",
    "ruinedfooocus": "RuinedFooocus",
    "tensorart": "TensorArt",
}


def origin_label(family: str) -> str | None:
    """Return the source-generator label for a replacement document."""
    return ORIGIN_LABELS.get(family)


def replacement_family(text: str) -> str | None:
    """Identify an opaque source from its stored JSON when its chunk name is lost."""
    document = first_document(text)
    if not isinstance(document, dict):
        return None
    if {"prompt", "uc", "noise_schedule"} <= set(document):
        return "novelai"
    if {"generation_mode", "positive_prompt", "negative_prompt"} <= set(document):
        return "invokeai"
    if {"prompt", "negativePrompt", "ksamplerName"} <= set(document):
        return "tensorart"
    return None


def empty_result() -> dict[str, Any]:
    """The common parsed shape, before a family fills it in."""
    return {
        "prompt": "",
        "negative_prompt": None,
        "fields": {},
        "field_order": [],
        "warnings": [],
    }


def coerce(name: str, value: Any) -> Any:
    """Give a value the type its field has everywhere else."""
    try:
        if name in INT_FIELDS:
            return int(value)
        if name in FLOAT_FIELDS:
            return float(value)
    except (TypeError, ValueError):
        return value
    return value


def put(result: dict[str, Any], name: str, value: Any) -> None:
    """Record one field, keeping the order it arrived in."""
    if value is None or value == "" or name in result["fields"]:
        return
    result["fields"][name] = coerce(name, value)
    result["field_order"].append(name)


def put_mapped(
    result: dict[str, Any], source: dict[str, Any], names: tuple[tuple[str, str], ...]
) -> None:
    """Record every field a family provides, under the name we already use."""
    for key, name in names:
        put(result, name, source.get(key))


def put_size(result: dict[str, Any], width: Any, height: Any) -> None:
    """``Size`` is one field here and two everywhere else."""
    try:
        if width and height:
            put(result, "Size", f"{int(width)}x{int(height)}")
    except (TypeError, ValueError):
        return


def first_document(text: str | None) -> Any:
    """The first JSON value in the text, ignoring whatever follows it.

    TensorArt writes a trailing NUL byte after its document, and a chunk that
    holds one good value followed by rubbish is still worth reading.
    """
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        value, _ = json.JSONDecoder().raw_decode(text.lstrip("﻿").strip("\x00").strip())
        return value
    except (json.JSONDecodeError, RecursionError):
        return None
