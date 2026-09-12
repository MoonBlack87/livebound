"""Turn a parsed infotext into the shape the UI renders.

The layout deliberately mirrors what CivitAI shows under an image - prompt,
negative prompt, a compact stats grid, then the resources - because that is the
comparison the user will make. Fields are grouped rather than dumped so the
important five are readable at a glance.
"""

from __future__ import annotations

from typing import Any

#: Rendered as the stat grid, in this order. The label is what CivitAI calls it.
PRIMARY_FIELDS: list[tuple[str, str]] = [
    ("Model", "Model"),
    ("Sampler", "Sampler"),
    ("Schedule type", "Scheduler"),
    ("Steps", "Steps"),
    ("CFG scale", "CFG scale"),
    ("Seed", "Seed"),
    ("Size", "Size"),
    ("Clip skip", "Clip skip"),
    ("Denoising strength", "Denoising"),
]

#: Shown in a collapsed "Hires" block when present.
HIRES_FIELDS = [
    "Hires upscale",
    "Hires upscaler",
    "Hires steps",
    "Hires CFG Scale",
    "Hires prompt",
    "Hires negative prompt",
]

#: Never rendered as a plain row: either shown as resource cards or pure noise.
_SUPPRESSED = {
    "Civitai resources",
    "Hashes",
    "Lora hashes",
    "TI hashes",
    "Model hash",
    "Prompt Enhancer",
}


def render(parsed: dict[str, Any], resources: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    fields = dict(parsed.get("fields") or {})

    primary = [
        {"key": key, "label": label, "value": _text(fields[key])}
        for key, label in PRIMARY_FIELDS
        if fields.get(key) not in (None, "")
    ]
    hires = [
        {"key": key, "label": key, "value": _text(fields[key])}
        for key in HIRES_FIELDS
        if fields.get(key) not in (None, "")
    ]

    shown = {item["key"] for item in primary} | {item["key"] for item in hires} | _SUPPRESSED
    other = [
        {"key": key, "label": key, "value": _text(fields[key])}
        for key in parsed.get("field_order") or []
        if key not in shown and fields.get(key) not in (None, "")
    ]

    return {
        "prompt": parsed.get("prompt") or "",
        "negative_prompt": parsed.get("negative_prompt"),
        "primary": primary,
        "hires": hires,
        "other": other,
        "resources": [_resource_card(item) for item in (resources or [])],
        "warnings": parsed.get("warnings") or [],
        "has_metadata": bool(parsed.get("prompt") or fields),
    }


def _resource_card(resource: dict[str, Any]) -> dict[str, Any]:
    from .. import config

    base = config.site_base()
    version_id = resource.get("model_version_id")
    model_id = resource.get("model_id")
    url = None
    if model_id and version_id:
        url = f"{base}/models/{model_id}?modelVersionId={version_id}"
    elif model_id:
        url = f"{base}/models/{model_id}"
    elif version_id:
        # The embedded `Civitai resources` array carries modelVersionId but often
        # no modelId. This route redirects (308) to the full model page, so a
        # resource is never left unlinkable just because the id is missing.
        url = f"{base}/model-versions/{version_id}"

    return {
        "type": resource.get("resource_type"),
        "name": resource.get("model_name") or resource.get("name_in_prompt"),
        "version": resource.get("version_name"),
        "prompt_name": resource.get("name_in_prompt"),
        "weight": resource.get("weight"),
        "hash": resource.get("hash"),
        "model_id": model_id,
        "model_version_id": version_id,
        "url": url,
        "resolved": bool(version_id),
        "thumbnail_url": resource.get("thumbnail_url"),
    }


def _text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        import json

        return json.dumps(value, ensure_ascii=False)
    return str(value)
