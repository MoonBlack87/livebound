"""Read ComfyUI's API graph without changing the file that carried it."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from . import families

FAMILY = "comfyui"
EXIF_PROMPT_KEY = "comfyui_prompt"
_SAMPLERS = {"KSampler", "KSamplerAdvanced"}
_MODEL_SUFFIXES = (".safetensors", ".ckpt", ".pt", ".bin", ".pth")
_MAX_DEPTH = 20
_FIELD_INPUTS = {
    "Steps": ("steps",),
    "CFG scale": ("cfg",),
    "Sampler": ("sampler_name",),
    "Schedule type": ("scheduler",),
    "Denoising strength": ("denoise",),
    "Seed": ("seed", "noise_seed", "Value"),
    "Clip skip": ("stop_at_clip_layer",),
}
_RESOURCE_INPUTS = {
    "Model": (
        (("CheckpointLoaderSimple", "CheckpointLoader"), "ckpt_name"),
        (("UNETLoader",), "unet_name"),
    ),
    "Hires upscaler": ((("UpscaleModelLoader",), "model_name"),),
    "VAE": ((("VAELoader",), "vae_name"),),
}


def raw_text(chunks: Mapping[str, str], user_comment: str | None) -> str | None:
    """Return the graph from its PNG, EXIF UserComment, or EXIF Model home."""
    return chunks.get("prompt") or chunks.get(EXIF_PROMPT_KEY) or user_comment


def detects(chunks: Mapping[str, str], user_comment: str | None) -> bool:
    """Whether the metadata holds a ComfyUI API graph, not its editor workflow."""
    return graph(raw_text(chunks, user_comment)) is not None


def read(chunks: Mapping[str, str], user_comment: str | None) -> dict[str, Any]:
    """Read the graph's generation facts into the common parsed shape."""
    return parse(raw_text(chunks, user_comment))


def parse(text: str | None) -> dict[str, Any]:
    """Read the first graph document generators saved in ``text``."""
    result = families.empty_result()
    nodes = graph(text)
    if nodes is None:
        return result

    sampler = _sampler(nodes)
    if sampler is not None:
        inputs = _inputs(sampler)
        latent = _node(nodes, inputs.get("latent_image"))
        result["prompt"] = _conditioning(nodes, inputs.get("positive"))
        negative = _conditioning(nodes, inputs.get("negative"))
        result["negative_prompt"] = negative or None
        for field, keys in _FIELD_INPUTS.items():
            if field == "Clip skip":
                continue
            value = next((inputs[key] for key in keys if key in inputs), None)
            families.put(
                result,
                field,
                value if field in ("Sampler", "Schedule type") else _number(nodes, value),
            )
        if latent is not None:
            latent_inputs = _inputs(latent)
            families.put_size(
                result,
                _number(nodes, latent_inputs.get("width")),
                _number(nodes, latent_inputs.get("height")),
            )

    loras: list[tuple[str, Any]] = []
    for node in nodes.values():
        kind = str(node.get("class_type") or "")
        inputs = _inputs(node)
        for field, locations in _RESOURCE_INPUTS.items():
            for kinds, key in locations:
                if kind in kinds:
                    families.put(
                        result, field, _model_name(_resource_name(nodes, inputs.get(key), key))
                    )
        if kind in ("LoraLoader", "LoraLoaderModelOnly"):
            strength = _number(nodes, inputs.get("strength_model"))
            if strength == 0:
                continue
            name = _model_name(_resource_name(nodes, inputs.get("lora_name"), "lora_name"))
            if name and strength is not None:
                loras.append((name, strength))
        elif kind == "CLIPSetLastLayer":
            layer = _number(nodes, inputs.get("stop_at_clip_layer"))
            if isinstance(layer, (int, float)):
                families.put(result, "Clip skip", -layer)

    if loras:
        families.put(
            result, "Lora weights", ", ".join(f"{name}: {weight}" for name, weight in loras)
        )
    return result


def project_prompts(
    text: str,
    original: dict[str, Any],
    effective: dict[str, Any],
    resource_rows: tuple[dict[str, Any], ...] = (),
    source_resource_rows: tuple[dict[str, Any], ...] = (),
) -> str:
    """Project every certain common value into the saved API graph.

    A parsed Comfy prompt can be assembled from several nodes.  Replacing such
    a summary would guess at the graph's meaning, so only an exact node value is
    changed; every other text remains the workflow's own text.
    """
    document = _document(text)
    if not isinstance(document, dict):
        return text
    graph_document = (
        document.get("prompt") if isinstance(document.get("prompt"), dict) else document
    )
    if not isinstance(graph_document, dict):
        return text

    replacements = (
        (original.get("prompt"), effective.get("prompt")),
        (original.get("negative_prompt"), effective.get("negative_prompt")),
    )
    changed = False
    for old, new in replacements:
        if not isinstance(old, str) or not old or old == new:
            continue
        replacement = new if isinstance(new, str) else ""
        for node in graph_document.values():
            inputs = _inputs(node) if isinstance(node, dict) else {}
            for key, value in inputs.items():
                if value == old and key in _PROMPT_INPUTS:
                    inputs[key] = replacement
                    changed = True
    before = original.get("fields") or {}
    fields = effective.get("fields") or {}
    for field, keys in _FIELD_INPUTS.items():
        old = before.get(field)
        if old == fields.get(field):
            continue
        replacement = fields.get(field)
        if field == "Clip skip" and isinstance(replacement, (int, float)):
            replacement = -replacement
        for node in graph_document.values():
            inputs = _inputs(node) if isinstance(node, dict) else {}
            for key in keys:
                if inputs.get(key) != old and not (
                    field == "Clip skip" and inputs.get(key) == -old
                ):
                    continue
                if field not in fields:
                    inputs.pop(key, None)
                else:
                    inputs[key] = replacement
                changed = True
    old_size = before.get("Size")
    if old_size != fields.get("Size"):
        old_width, old_height = _dimensions(old_size)
        new_width, new_height = _dimensions(fields.get("Size"))
        for node in graph_document.values():
            inputs = _inputs(node) if isinstance(node, dict) else {}
            dimensions = (("width", old_width, new_width), ("height", old_height, new_height))
            for key, old, new in dimensions:
                if inputs.get(key) != old:
                    continue
                if "Size" not in fields:
                    inputs.pop(key, None)
                else:
                    inputs[key] = new
                changed = True
    for field, locations in _RESOURCE_INPUTS.items():
        old_value = before.get(field)
        if old_value == fields.get(field):
            continue
        for node in graph_document.values():
            inputs = _inputs(node) if isinstance(node, dict) else {}
            kind = str(node.get("class_type") or "") if isinstance(node, dict) else ""
            for kinds, key in locations:
                if kind not in kinds or _model_name(
                    _resource_name(graph_document, inputs.get(key), key)
                ) != old_value:
                    continue
                if field not in fields:
                    inputs.pop(key, None)
                else:
                    inputs[key] = fields[field]
                changed = True
    if source_resource_rows:
        loras = {
            _model_name(str(row.get("name_in_prompt") or "")): row
            for row in resource_rows
            if row.get("resource_type") == "lora" and row.get("name_in_prompt")
        }
        for node in graph_document.values():
            if not isinstance(node, dict) or node.get("class_type") not in (
                "LoraLoader",
                "LoraLoaderModelOnly",
            ):
                continue
            inputs = _inputs(node)
            name = _model_name(
                _resource_name(graph_document, inputs.get("lora_name"), "lora_name")
            )
            row = loras.pop(name, None)
            if row is None:
                inputs.pop("lora_name", None)
                inputs.pop("strength_model", None)
                inputs.pop("strength_clip", None)
                changed = True
                continue
            if row.get("weight") is None:
                continue
            for key in ("strength_model", "strength_clip"):
                if key in inputs and inputs[key] != row["weight"]:
                    inputs[key] = row["weight"]
                    changed = True
        for row in loras.values():
            version_id = row.get("model_version_id")
            if not isinstance(version_id, int):
                continue
            node_id = _new_node_id(graph_document, "LiveboundLora")
            graph_document[node_id] = {
                "class_type": "LoraLoader",
                "inputs": {
                    "lora_name": _air("lora", version_id),
                    "strength_model": row.get("weight", 1),
                    "strength_clip": row.get("weight", 1),
                },
            }
            changed = True
    return json.dumps(document, ensure_ascii=False) if changed else text


def graph(text: str | None) -> dict[str, dict[str, Any]] | None:
    """Return the API graph from a bare graph or a ``prompt`` envelope."""
    document = _document(text)
    if not isinstance(document, dict):
        return None
    candidate = document.get("prompt") if isinstance(document.get("prompt"), dict) else document
    if not isinstance(candidate, dict):
        return None
    nodes = {
        str(node_id): node
        for node_id, node in candidate.items()
        if isinstance(node, dict) and isinstance(node.get("inputs"), dict)
    }
    return nodes if any(node.get("class_type") for node in nodes.values()) else None


def document(text: str | None) -> dict[str, Any] | None:
    """Expose the saved outer document to the CivitAI-specific adapter."""
    value = _document(text)
    return value if isinstance(value, dict) else None


def _document(text: str | None) -> Any:
    if not text:
        return None
    try:
        # Python accepts these JavaScript constants by default. Replacing them
        # with no value keeps a damaged widget from losing the useful graph.
        return json.loads(text, parse_constant=lambda _constant: None)
    except (json.JSONDecodeError, RecursionError):
        return None


def _inputs(node: Mapping[str, Any] | None) -> dict[str, Any]:
    value = node.get("inputs") if node else None
    return value if isinstance(value, dict) else {}


def _dimensions(value: Any) -> tuple[int | None, int | None]:
    width, separator, height = str(value or "").partition("x")
    if not separator:
        return None, None
    try:
        return int(width), int(height)
    except ValueError:
        return None, None


def _node(nodes: Mapping[str, dict[str, Any]], value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict) and isinstance(value.get("inputs"), dict):
        return value
    if isinstance(value, list) and len(value) >= 2:
        return nodes.get(str(value[0]))
    return None


def _sampler(nodes: Mapping[str, dict[str, Any]]) -> dict[str, Any] | None:
    found = [node for node in nodes.values() if node.get("class_type") in _SAMPLERS]
    return next(
        (
            node
            for node in found
            if (_node(nodes, _inputs(node).get("latent_image")) or {}).get("class_type")
            == "EmptyLatentImage"
        ),
        found[0] if found else None,
    )


def _number(
    nodes: Mapping[str, dict[str, Any]], value: Any, names: tuple[str, ...] = ("Value",)
) -> Any:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    current = value
    seen: set[int] = set()
    for _ in range(_MAX_DEPTH):
        node = _node(nodes, current)
        if node is None or id(node) in seen:
            return current if isinstance(current, (int, float)) else None
        seen.add(id(node))
        inputs = _inputs(node)
        next_value = next((inputs[name] for name in names if name in inputs), None)
        if isinstance(next_value, (int, float)) and not isinstance(next_value, bool):
            return next_value
        current = next_value
    return None


#: Where a node keeps its prompt text. The first two are ComfyUI's own; the rest
#: are what custom nodes in the reference corpus call the same thing - rgthree's
#: Power Prompt writes `prompt`, Comfyroll's text box `Text`, and a multiline
#: primitive feeding an encoder `value`. Three of eighteen corpus graphs carry
#: their prompt nowhere else. Only a node the conditioning link actually leads
#: to is read, so this follows the graph's own wiring rather than hunting the
#: document for something that looks like a prompt.
_TEXT_KEYS = ("populated_text", "text", "prompt", "Text", "value", "string")
_PROMPT_INPUTS = _TEXT_KEYS + ("text_g", "text_l")


def _conditioning(
    nodes: Mapping[str, dict[str, Any]], value: Any, seen: set[int] | None = None
) -> str:
    seen = seen if seen is not None else set()
    current = value
    for _ in range(_MAX_DEPTH):
        if isinstance(current, str):
            return current
        node = _node(nodes, current)
        if node is None or id(node) in seen:
            return ""
        seen.add(id(node))
        inputs = _inputs(node)
        if node.get("class_type") in ("ControlNetApply", "FluxGuidance"):
            current = inputs.get("conditioning")
            continue
        for key in _TEXT_KEYS:
            text = _conditioning_value(nodes, inputs.get(key), seen)
            if text:
                return text
        global_text = _conditioning_value(nodes, inputs.get("text_g"), seen)
        local_text = _conditioning_value(nodes, inputs.get("text_l"), seen)
        if global_text:
            return (
                global_text
                if not local_text or local_text == global_text
                else f"{global_text}, {local_text}"
            )
        current = inputs.get("text_positive") or inputs.get("text_negative")
    return ""


def _conditioning_value(nodes: Mapping[str, dict[str, Any]], value: Any, seen: set[int]) -> str:
    if isinstance(value, str):
        return value
    node = _node(nodes, value)
    if node is None or id(node) in seen:
        return ""
    return _conditioning(nodes, value, seen)


def _resource_name(nodes: Mapping[str, dict[str, Any]], value: Any, key: str) -> str:
    """The name behind a resource widget, which may be a link to another node.

    A link is ``[node id, output slot]``, and the slot is part of the answer. A
    picker that hands a checkpoint out of one output and an upscaler out of the
    next has one node and two names; reading only its first would make the
    upscaler the checkpoint - an identity that then goes out as a credit. So a
    slot this node does not explain yields nothing at all. A resource we cannot
    name is a gap the user can see and fill; a resource named wrongly is not.
    """
    current = value
    seen: set[int] = set()
    for _ in range(_MAX_DEPTH):
        if isinstance(current, str):
            return current
        slot = current[1] if isinstance(current, (list, tuple)) and len(current) > 1 else 0
        node = _node(nodes, current)
        if node is None or id(node) in seen:
            return ""
        seen.add(id(node))
        inputs = _inputs(node)
        candidate = next(
            (inputs[name] for name in (key, "value", "string", "air") if name in inputs),
            None,
        )
        names = isinstance(candidate, (list, tuple)) and all(
            isinstance(part, str) for part in candidate
        )
        if names:
            # One name per output slot: the node explains which is which.
            if not isinstance(slot, int) or slot >= len(candidate):
                return ""
            current = candidate[slot]
            continue
        if slot:
            return ""
        current = candidate
    return ""


def _model_name(value: str) -> str:
    name = re.split(r"[\\/]", value or "")[-1].strip()
    for suffix in _MODEL_SUFFIXES:
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name


def _new_node_id(nodes: Mapping[str, Any], prefix: str) -> str:
    index = 1
    while f"{prefix}{index}" in nodes:
        index += 1
    return f"{prefix}{index}"


def _air(kind: str, version_id: int) -> str:
    return f"urn:air:other:{kind}:civitai:0@{version_id}"
