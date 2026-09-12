"""Read-only adapter for RuinedFooocus metadata.

RuinedFooocus writes flat JSON into the same ``parameters`` chunk A1111 uses and
names itself in it::

    {"Prompt": "...", "Negative": "...", "steps": 30, "cfg": 7.0,
     "base_model_name": "sdxl.safetensors", "base_model_hash": "abc123",
     "software": "RuinedFooocus"}

As for SwarmUI, its names are mapped onto the ones this application already
carries rather than added beside them. The model name arrives with its file
extension, which no other family includes, so it is dropped - a model called
``sdxl`` and one called ``sdxl.safetensors`` would otherwise be two models.

Its ``loras`` field packs a hash together with a weight and a path, two values
in one string::

    [["BFA3BB4FBE", "1.0 - moi\\helios-beasts.safetensors"]]

CivitAI's own parser leaves that key in its untouched remainder, which is why a
RuinedFooocus image uploaded there arrives with its prompt and without its
resources. Read defensively here: the sample this was built from is two years
old, so an entry that does not have this shape is skipped rather than warned
about.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Mapping
from typing import Any

from . import families

FAMILY = "ruinedfooocus"
PARAMETERS_KEYWORD = "parameters"

#: Whitespace-tolerant because the writer's JSON encoder decides the spacing.
_MARKER_RE = re.compile(r'"software"\s*:\s*"RuinedFooocus"')

_FIELD_NAMES: tuple[tuple[str, str], ...] = (
    ("steps", "Steps"),
    ("sampler_name", "Sampler"),
    ("scheduler", "Schedule type"),
    ("cfg", "CFG scale"),
    ("seed", "Seed"),
    ("denoise", "Denoising strength"),
    ("base_model_hash", "Model hash"),
)

_INT_FIELDS = {"Steps", "Seed"}
_FLOAT_FIELDS = {"CFG scale", "Denoising strength"}

#: RuinedFooocus writes this when no scheduler was chosen; A1111 writes nothing.
_SCHEDULER_DEFAULT = "simple"


def detects(chunks: Mapping[str, str], user_comment: str | None) -> bool:
    """Whether the container metadata has RuinedFooocus's shape."""
    return bool(_MARKER_RE.search(chunks.get(PARAMETERS_KEYWORD) or ""))


def raw_text(chunks: Mapping[str, str], user_comment: str | None) -> str | None:
    """Read the source text from the container location used by this family."""
    return chunks.get(PARAMETERS_KEYWORD)


def read(chunks: Mapping[str, str], user_comment: str | None) -> dict[str, Any]:
    """Read this family's container metadata into the common parsed shape."""
    return parse(raw_text(chunks, user_comment))


def parse(text: str | None) -> dict[str, Any]:
    """Parse RuinedFooocus JSON into the common parsed shape."""
    result: dict[str, Any] = {
        "prompt": "",
        "negative_prompt": None,
        "fields": {},
        "field_order": [],
        "warnings": [],
    }
    if not text:
        return result

    document = json.loads(text)
    if not isinstance(document, dict):
        result["warnings"].append("RuinedFooocus metadata is not an object")
        return result

    result["prompt"] = str(document.get("Prompt") or "")
    negative = document.get("Negative")
    result["negative_prompt"] = str(negative) if negative else None

    for source, name in _FIELD_NAMES:
        value = document.get(source)
        if value is None or value == "":
            continue
        if name == "Schedule type" and value == _SCHEDULER_DEFAULT:
            continue
        result["fields"][name] = _coerce(name, value)
        result["field_order"].append(name)

    model = document.get("base_model_name")
    if model:
        result["fields"]["Model"] = str(model).rsplit(".", 1)[0]
        result["field_order"].append("Model")

    width, height = document.get("width"), document.get("height")
    if width and height:
        result["fields"]["Size"] = f"{int(width)}x{int(height)}"
        result["field_order"].append("Size")

    _read_loras(document.get("loras"), result)
    return result


def _read_loras(value: Any, result: dict[str, Any]) -> None:
    """Map the packed ``loras`` string onto the field `resources` derives from."""
    entries: Any = value
    if isinstance(value, str):
        # Older writers put Python's `str` of the list here instead of an array.
        try:
            entries = ast.literal_eval(value) if value.strip() else None
        except (ValueError, SyntaxError):
            return
    if not isinstance(entries, list):
        return

    hashes: dict[str, str] = {}
    weights: dict[str, str] = {}
    for entry in entries:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            continue
        digest, packed = str(entry[0]).strip(), str(entry[1])
        # `"1.0 - moi\\helios-beasts.safetensors"`: weight, separator, path.
        weight, separator, name = packed.partition(" - ")
        if not separator:
            weight, name = "", packed
        name = re.split(r"[\\/]", name)[-1].rsplit(".", 1)[0].strip()
        if digest and name:
            hashes[name] = digest
            if weight.strip():
                weights[name] = weight.strip()

    if hashes:
        families.put(
            result, "Lora hashes", ", ".join(f"{n}: {d}" for n, d in hashes.items())
        )
    if weights:
        families.put(
            result, "Lora weights", ", ".join(f"{n}: {w}" for n, w in weights.items())
        )


def _coerce(name: str, value: Any) -> Any:
    """Give a value the type its field has everywhere else."""
    try:
        if name in _INT_FIELDS:
            return int(value)
        if name in _FLOAT_FIELDS:
            return float(value)
    except (TypeError, ValueError):
        return value
    return value
