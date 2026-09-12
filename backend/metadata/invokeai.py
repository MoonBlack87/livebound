"""Read-only adapter for InvokeAI metadata.

InvokeAI writes two chunks: ``invokeai_metadata`` with the generation, and
``invokeai_graph`` with the node graph that produced it. Only the first is read;
the graph is InvokeAI's own business and says nothing the first does not.

Its model hash is ``blake3:...``. That is deliberately **not** recorded as a
``Model hash``: every hash this application matches against - CivitAI's cache
and the local model inventory - is a SHA256, so a blake3 digest under that name
would produce a resource row that can never resolve and an "unrecognised" mark
nobody can act on. The model's name is kept instead.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import families

FAMILY = "invokeai"
METADATA_KEYWORD = "invokeai_metadata"

_FIELD_NAMES: tuple[tuple[str, str], ...] = (
    ("steps", "Steps"),
    ("scheduler", "Sampler"),
    ("cfg_scale", "CFG scale"),
    ("seed", "Seed"),
)


def detects(chunks: Mapping[str, str], user_comment: str | None) -> bool:
    """Whether the container metadata has InvokeAI's shape."""
    return bool(chunks.get(METADATA_KEYWORD))


def raw_text(chunks: Mapping[str, str], user_comment: str | None) -> str | None:
    """Read the source text from the container location used by this family."""
    return chunks.get(METADATA_KEYWORD)


def read(chunks: Mapping[str, str], user_comment: str | None) -> dict[str, Any]:
    """Read this family's container metadata into the common parsed shape."""
    result = families.empty_result()
    document = families.first_document(chunks.get(METADATA_KEYWORD))
    if not isinstance(document, dict):
        return result

    result["prompt"] = str(document.get("positive_prompt") or "")
    negative = document.get("negative_prompt")
    result["negative_prompt"] = str(negative).strip() or None if negative else None

    families.put_mapped(result, document, _FIELD_NAMES)
    families.put_size(result, document.get("width"), document.get("height"))

    model = document.get("model")
    if isinstance(model, dict):
        families.put(result, "Model", model.get("name") or model.get("key"))
    return result
