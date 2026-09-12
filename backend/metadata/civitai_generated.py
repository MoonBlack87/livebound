"""Read the ComfyUI graph and resource identities CivitAI writes on-site."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from . import comfyui, families

FAMILY = "civitai"
_AIR_RE = re.compile(
    r"^urn:air:(?P<ecosystem>[A-Za-z0-9_\-/]+):"
    r"(?P<kind>[A-Za-z0-9_\-/]+):civitai:(?P<model_id>\d+)@(?P<version_id>\d+)",
    re.IGNORECASE,
)
_EXTRA_FIELDS = (
    ("steps", "Steps"),
    ("sampler", "Sampler"),
    ("cfgScale", "CFG scale"),
    ("seed", "Seed"),
    ("clipSkip", "Clip skip"),
    ("denoise", "Denoising strength"),
)


def raw_text(chunks: Mapping[str, str], user_comment: str | None) -> str | None:
    return comfyui.raw_text(chunks, user_comment)


def detects(chunks: Mapping[str, str], user_comment: str | None) -> bool:
    """A graph, the request that produced it, **and** CivitAI's own identifiers.

    All three, because the first two alone describe an ordinary ComfyUI image
    that some save node happened to attach a summary to. Claiming that one here
    would answer it from the summary and drop what only the graph holds - its
    model, its LoRAs, every field the summary does not repeat.
    """
    text = raw_text(chunks, user_comment)
    document = comfyui.document(text)
    extra = _extra(document)
    if comfyui.graph(text) is None or extra is None:
        return False
    return bool(_credits(document, extra))


def read(chunks: Mapping[str, str], user_comment: str | None) -> dict[str, Any]:
    return parse(raw_text(chunks, user_comment))


def parse(text: str | None) -> dict[str, Any]:
    """Prefer CivitAI's requested values over the graph it assembled for them."""
    result = families.empty_result()
    document = comfyui.document(text)
    extra = _extra(document)
    if extra is None:
        return result

    result["prompt"] = str(extra.get("prompt") or "")
    negative = extra.get("negativePrompt")
    result["negative_prompt"] = str(negative) if negative else None
    families.put_mapped(result, extra, _EXTRA_FIELDS)
    families.put_size(result, extra.get("width"), extra.get("height"))

    # The identity of every resource, and the only one these files carry. No
    # `Model` and no `Version` are written beside it: this image has no
    # generator version, and a model version id is not one. What follows from
    # having neither is settled in `upload_document.render_meta`.
    families.put(result, "Civitai resources", _credits(document, extra))
    return result


def project(
    text: str,
    original: dict[str, Any],
    effective: dict[str, Any],
    resource_rows: tuple[dict[str, Any], ...],
    source_resource_rows: tuple[dict[str, Any], ...],
) -> str:
    """Project the authoritative CivitAI request and its matching graph."""
    projected = comfyui.project_prompts(
        text, original, effective, resource_rows, source_resource_rows
    )
    document = comfyui.document(projected)
    extra = _extra(document)
    if document is None or extra is None:
        return projected

    changed = False
    if original.get("prompt") != effective.get("prompt"):
        extra["prompt"] = effective.get("prompt") or ""
        changed = True
    if original.get("negative_prompt") != effective.get("negative_prompt"):
        extra["negativePrompt"] = effective.get("negative_prompt") or ""
        changed = True
    before = original.get("fields") or {}
    fields = effective.get("fields") or {}
    for key, field in _EXTRA_FIELDS:
        if before.get(field) == fields.get(field):
            continue
        if field in fields:
            extra[key] = fields[field]
        else:
            extra.pop(key, None)
        changed = True
    if before.get("Size") != fields.get("Size"):
        dimensions = _dimensions(fields.get("Size"))
        if dimensions is None:
            extra.pop("width", None)
            extra.pop("height", None)
        else:
            extra["width"], extra["height"] = dimensions
        changed = True

    if source_resource_rows:
        credits = [
            {
                "modelVersionId": row["model_version_id"],
                **({"strength": row["weight"]} if row.get("weight") is not None else {}),
            }
            for row in resource_rows
            if isinstance(row.get("model_version_id"), int)
        ]
        extra["resources"] = credits
        outer_extra = document.setdefault("extra", {})
        if isinstance(outer_extra, dict):
            previous = {
                _version(air): air
                for air in outer_extra.get("airs", [])
                if isinstance(air, str) and _version(air) is not None
            }
            outer_extra["airs"] = [
                previous.get(row["model_version_id"])
                or _air(str(row.get("resource_type") or "checkpoint"), row["model_version_id"])
                for row in resource_rows
                if isinstance(row.get("model_version_id"), int)
            ]
        changed = True
    if not changed:
        return projected
    if isinstance(document.get("extraMetadata"), str):
        document["extraMetadata"] = json.dumps(extra, ensure_ascii=False)
    return json.dumps(document, ensure_ascii=False)


def _extra(document: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(document, dict):
        return None
    raw = document.get("extraMetadata")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, RecursionError):
            return None
    return raw if isinstance(raw, dict) else None


def _dimensions(value: Any) -> tuple[int, int] | None:
    width, separator, height = str(value or "").partition("x")
    if not separator:
        return None
    try:
        return int(width), int(height)
    except ValueError:
        return None


def _version(air: str) -> int | None:
    match = _AIR_RE.match(air)
    return int(match.group("version_id")) if match is not None else None


def _air(kind: str, version_id: int) -> str:
    return f"urn:air:other:{kind}:civitai:0@{version_id}"


def _credits(document: dict[str, Any] | None, extra: dict[str, Any]) -> list[dict[str, Any]]:
    outer = document or {}
    outer_extra = outer.get("extra")
    airs = outer_extra.get("airs") if isinstance(outer_extra, dict) else []
    strengths: dict[int, Any] = {}
    resources = extra.get("resources")
    if isinstance(resources, list):
        for resource in resources:
            if not isinstance(resource, dict):
                continue
            version = resource.get("modelVersionId")
            if isinstance(version, int):
                strengths[version] = resource.get("strength", resource.get("weight"))

    credits: list[dict[str, Any]] = []
    for air in airs if isinstance(airs, list) else []:
        match = _AIR_RE.match(air) if isinstance(air, str) else None
        if match is None:
            continue
        version = int(match.group("version_id"))
        if any(credit["modelVersionId"] == version for credit in credits):
            continue
        credit: dict[str, Any] = {"type": match.group("kind").lower(), "modelVersionId": version}
        if version in strengths and strengths[version] is not None:
            credit["weight"] = strengths[version]
        credits.append(credit)
    return credits
