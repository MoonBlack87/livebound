"""Read-only adapter for TensorArt metadata.

TensorArt writes ``generation_data`` with the generation and a ``prompt`` chunk
holding a ComfyUI graph - it runs ComfyUI underneath. The first is read; the
graph is left to whatever reads ComfyUI graphs, and a family that claimed both
would have to agree with that reader for ever.

Its document is followed by a trailing NUL byte, so it is read as "the first
value in this text" rather than as "this text is JSON".
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import families

FAMILY = "tensorart"
GENERATION_KEYWORD = "generation_data"

_FIELD_NAMES: tuple[tuple[str, str], ...] = (
    ("steps", "Steps"),
    ("ksamplerName", "Sampler"),
    ("schedule", "Schedule type"),
    ("cfgScale", "CFG scale"),
    ("seed", "Seed"),
    ("clipSkip", "Clip skip"),
)


def detects(chunks: Mapping[str, str], user_comment: str | None) -> bool:
    """Whether the container metadata has TensorArt's shape."""
    return bool(chunks.get(GENERATION_KEYWORD))


def raw_text(chunks: Mapping[str, str], user_comment: str | None) -> str | None:
    """Read the source text from the container location used by this family."""
    return chunks.get(GENERATION_KEYWORD)


def read(chunks: Mapping[str, str], user_comment: str | None) -> dict[str, Any]:
    """Read this family's container metadata into the common parsed shape."""
    result = families.empty_result()
    document = families.first_document(chunks.get(GENERATION_KEYWORD))
    if not isinstance(document, dict):
        return result

    result["prompt"] = str(document.get("prompt") or "")
    negative = document.get("negativePrompt")
    result["negative_prompt"] = str(negative).strip() or None if negative else None

    families.put_mapped(result, document, _FIELD_NAMES)
    families.put_size(result, document.get("width"), document.get("height"))

    base = document.get("baseModel")
    if isinstance(base, dict):
        families.put(result, "Model", base.get("label") or base.get("modelId"))
    return result
