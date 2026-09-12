"""Read-only adapter for SwarmUI metadata.

SwarmUI writes JSON into the same ``parameters`` chunk that A1111 uses, with
everything under one key::

    {"sui_image_params": {"prompt": "...", "negativeprompt": "...",
                          "steps": 30, "cfgscale": 7.0, "model": "sdxl_base"},
     "sui_models": [{"name": "x.safetensors", "param": "loras", "hash": "0x…"}]}

Its field names are its own (``cfgscale``, ``negativeprompt``), so they are
mapped onto the names this application already carries - the ones an A1111
infotext produces and the metadata settings already list. A second vocabulary
for the same facts would show up twice in the field inventory, twice in the
exclusion settings, and would have to be kept in step for ever.

``sui_models`` is worth more here than anywhere else. It carries the **file's**
SHA256 - the same digest this application stores for a local model file, unlike
A1111, which writes a LoRA hash taken over the tensor data and can therefore
never match. So those entries are mapped onto ``Model``, ``Model hash`` and
``Lora hashes``, the field names :mod:`.resources` already derives from, and a
SwarmUI image resolves its resources by hash with nothing added there.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from . import families

FAMILY = "swarmui"
PARAMETERS_KEYWORD = "parameters"

#: The marker SwarmUI itself documents; nothing else writes this key.
_MARKER = "sui_image_params"

#: SwarmUI's name for a value, and ours. Ours are the names already in the field
#: inventory, so nothing new appears in the settings for a fact we already had.
_FIELD_NAMES: tuple[tuple[str, str], ...] = (
    ("steps", "Steps"),
    ("sampler", "Sampler"),
    ("scheduler", "Schedule type"),
    ("cfgscale", "CFG scale"),
    ("seed", "Seed"),
    ("model", "Model"),
    ("swarm_version", "Version"),
)

_INT_FIELDS = {"Steps", "Seed"}
_FLOAT_FIELDS = {"CFG scale"}


def detects(chunks: Mapping[str, str], user_comment: str | None) -> bool:
    """Whether the container metadata has SwarmUI's shape."""
    return _MARKER in (chunks.get(PARAMETERS_KEYWORD) or "")


def raw_text(chunks: Mapping[str, str], user_comment: str | None) -> str | None:
    """Read the source text from the container location used by this family."""
    return chunks.get(PARAMETERS_KEYWORD)


def read(chunks: Mapping[str, str], user_comment: str | None) -> dict[str, Any]:
    """Read this family's container metadata into the common parsed shape."""
    return parse(raw_text(chunks, user_comment))


def parse(text: str | None) -> dict[str, Any]:
    """Parse SwarmUI JSON into ``{prompt, negative_prompt, fields, field_order, warnings}``."""
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
    params = document.get(_MARKER) if isinstance(document, dict) else None
    if not isinstance(params, dict):
        result["warnings"].append("SwarmUI metadata without sui_image_params")
        return result

    result["prompt"] = str(params.get("prompt") or "")
    negative = params.get("negativeprompt")
    result["negative_prompt"] = str(negative) if negative else None

    for source, name in _FIELD_NAMES:
        value = params.get(source)
        if value is None or value == "":
            continue
        result["fields"][name] = _coerce(name, value)
        result["field_order"].append(name)

    width, height = params.get("width"), params.get("height")
    if width and height:
        result["fields"]["Size"] = f"{int(width)}x{int(height)}"
        result["field_order"].append("Size")

    _read_models(document, params, result)
    return result


def project(
    text: str,
    original: dict[str, Any],
    effective: dict[str, Any],
    resource_rows: tuple[dict[str, Any], ...] = (),
) -> str:
    """Put changed common fields back into SwarmUI's own document."""
    document = json.loads(text)
    params = document.get(_MARKER) if isinstance(document, dict) else None
    if not isinstance(params, dict):
        return text

    fields = effective.get("fields") or {}
    before = original.get("fields") or {}
    if effective.get("prompt") != original.get("prompt"):
        params["prompt"] = effective.get("prompt") or ""
    if effective.get("negative_prompt") != original.get("negative_prompt"):
        params["negativeprompt"] = effective.get("negative_prompt") or ""
    for source, name in _FIELD_NAMES:
        if fields.get(name) == before.get(name):
            continue
        if name in fields:
            params[source] = fields[name]
        else:
            params.pop(source, None)
    if fields.get("Size") != before.get("Size") and "Size" in fields:
        width, _, height = str(fields["Size"]).partition("x")
        if width.isdigit() and height.isdigit():
            params["width"], params["height"] = int(width), int(height)
    elif "Size" not in fields and "Size" in before:
        params.pop("width", None)
        params.pop("height", None)
    _project_models(document, fields, before, resource_rows)
    return json.dumps(document, ensure_ascii=False)


def _project_models(
    document: dict[str, Any],
    fields: dict[str, Any],
    before: dict[str, Any],
    resource_rows: tuple[dict[str, Any], ...],
) -> None:
    """Apply resource assignments in the ``sui_models`` rows that state them."""
    models = document.get("sui_models")
    if not isinstance(models, list):
        return
    loras = {
        _normalise_name(str(row.get("name_in_prompt") or "")): row
        for row in resource_rows
        if row.get("resource_type") == "lora" and row.get("name_in_prompt")
    }
    model_hash_changed = fields.get("Model hash") != before.get("Model hash")
    kept: list[Any] = []
    for entry in models:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        kind = entry.get("param")
        name = _normalise_name(str(entry.get("name") or ""))
        if kind == "loras":
            row = loras.pop(name, None)
            if row is None:
                continue
            digest = row.get("hash")
            if digest:
                entry["hash"] = _with_prefix(str(entry.get("hash") or ""), str(digest))
            else:
                entry.pop("hash", None)
            if row.get("weight") is None:
                entry.pop("weight", None)
            else:
                entry["weight"] = row["weight"]
        elif kind == "model":
            if fields.get("Model") != before.get("Model"):
                if "Model" not in fields:
                    continue
                existing_name = str(entry.get("name") or "")
                suffix = ".safetensors" if existing_name.endswith(".safetensors") else ""
                entry["name"] = f"{fields['Model']}{suffix}"
            if model_hash_changed:
                digest = fields.get("Model hash")
                if digest is None:
                    entry.pop("hash", None)
                else:
                    entry["hash"] = _with_prefix(str(entry.get("hash") or ""), str(digest))
        kept.append(entry)
    for row in loras.values():
        digest = row.get("hash")
        if not digest:
            continue
        entry: dict[str, Any] = {
            "name": f"{row['name_in_prompt']}.safetensors",
            "param": "loras",
            "hash": str(digest),
        }
        if row.get("weight") is not None:
            entry["weight"] = row["weight"]
        kept.append(entry)
    document["sui_models"] = kept
    params = document.get(_MARKER)
    if not isinstance(params, dict):
        return
    lora_entries = [
        entry for entry in kept if isinstance(entry, dict) and entry.get("param") == "loras"
    ]
    if lora_entries:
        params["loras"] = [entry.get("name") for entry in lora_entries]
        params["loraweights"] = [entry.get("weight") for entry in lora_entries]
    else:
        params.pop("loras", None)
        params.pop("loraweights", None)


def _with_prefix(existing: str, digest: str) -> str:
    return f"0x{digest}" if existing.lower().startswith("0x") else digest


def _normalise_name(value: str) -> str:
    return value.rsplit(".", 1)[0].rsplit("/", 1)[-1].casefold()


def _stated_weights(params: dict[str, Any]) -> dict[str, Any]:
    """``loras`` and ``loraweights`` are two lists that belong together.

    The weight is not in ``sui_models`` beside the hash; it is in a parallel
    array under the parameters, matched by position. The names there carry the
    folder they were found in, so they are shortened the same way.
    """
    names = params.get("loras")
    weights = params.get("loraweights")
    if not isinstance(names, list) or not isinstance(weights, list):
        return {}
    out: dict[str, Any] = {}
    for index, name in enumerate(names):
        if index >= len(weights):
            break
        short = str(name).rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if short:
            out[short] = weights[index]
    return out


def _read_models(document: Any, params: dict[str, Any], result: dict[str, Any]) -> None:
    """Map ``sui_models`` onto the resource fields this application derives from."""
    models = document.get("sui_models") if isinstance(document, dict) else None
    if not isinstance(models, list):
        return

    loras: dict[str, str] = {}
    weights: dict[str, Any] = dict(_stated_weights(params))
    for entry in models:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").rsplit(".", 1)[0]
        digest = _digest(entry.get("hash"))
        if not name or not digest:
            continue
        kind = entry.get("param")
        if kind == "loras":
            # The name may carry the folder it was found in; the hash is what
            # identifies it, and a folder is one machine's arrangement.
            short = name.rsplit("/", 1)[-1]
            loras[short] = digest
            if entry.get("weight") is not None:
                weights[short] = entry["weight"]
        elif kind == "model":
            families.put(result, "Model hash", digest)

    if loras:
        families.put(
            result, "Lora hashes", ", ".join(f"{n}: {d}" for n, d in loras.items())
        )
    if weights:
        families.put(
            result, "Lora weights", ", ".join(f"{n}: {w}" for n, w in weights.items())
        )


def _digest(value: Any) -> str:
    """SwarmUI writes a full SHA256 with an ``0x`` prefix; ours carry neither."""
    text = str(value or "").strip()
    return text[2:] if text.lower().startswith("0x") else text


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
