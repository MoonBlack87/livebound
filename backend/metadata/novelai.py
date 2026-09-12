"""Read-only adapter for NovelAI metadata.

NovelAI writes its own PNG chunks - ``Software: NovelAI``, ``Source`` naming the
model and its hash, ``Description`` holding the prompt, and ``Comment`` holding
everything as JSON::

    Software: NovelAI
    Source:   NovelAI Diffusion V4.5 4BDE2A90
    Comment:  {"prompt": "...", "uc": "...", "steps": 28, "scale": 5.0, ...}

In a JPEG or WebP the same document arrives in EXIF UserComment wrapped in an
envelope, ``{"Comment": "{...}"}``, because EXIF has one field and NovelAI has
several. Both shapes are read here.

`scale` is what everyone else calls CFG, `uc` is the negative prompt, and
`noise_schedule` is the schedule. The v4 prompt objects are deliberately not
read: they carry the same text in a nested per-character form, and a rudimentary
reading that repeats the prompt twice is worse than one that does not.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import families

FAMILY = "novelai"
SOFTWARE_KEYWORD = "Software"
COMMENT_KEYWORD = "Comment"
SOURCE_KEYWORD = "Source"
PARAMETERS_KEYWORD = "parameters"

_FIELD_NAMES: tuple[tuple[str, str], ...] = (
    ("steps", "Steps"),
    ("sampler", "Sampler"),
    ("noise_schedule", "Schedule type"),
    ("scale", "CFG scale"),
    ("seed", "Seed"),
)


def detects(chunks: Mapping[str, str], user_comment: str | None) -> bool:
    """Whether the container metadata has NovelAI's shape."""
    if str(chunks.get(SOFTWARE_KEYWORD) or "").startswith("NovelAI"):
        return True
    return _envelope(user_comment) is not None


def raw_text(chunks: Mapping[str, str], user_comment: str | None) -> str | None:
    """Read the source text from the container location used by this family."""
    return chunks.get(COMMENT_KEYWORD) or user_comment


def read(chunks: Mapping[str, str], user_comment: str | None) -> dict[str, Any]:
    """Read this family's container metadata into the common parsed shape."""
    result = families.empty_result()
    document = families.first_document(chunks.get(COMMENT_KEYWORD)) or _envelope(user_comment)
    if not isinstance(document, dict):
        return result

    result["prompt"] = str(document.get("prompt") or chunks.get("Description") or "")
    negative = document.get("uc")
    result["negative_prompt"] = str(negative) if negative else None

    families.put_mapped(result, document, _FIELD_NAMES)
    families.put_size(result, document.get("width"), document.get("height"))
    _read_source(chunks.get(SOURCE_KEYWORD), result)
    return result


def _envelope(user_comment: str | None) -> dict[str, Any] | None:
    """JPEG and WebP carry the document inside ``{"Comment": "{...}"}``."""
    outer = families.first_document(user_comment)
    if not isinstance(outer, dict):
        return None
    inner = outer.get(COMMENT_KEYWORD)
    if isinstance(inner, str):
        inner = families.first_document(inner)
    if isinstance(inner, dict) and "prompt" in inner:
        return inner
    return None


def _read_source(source: str | None, result: dict[str, Any]) -> None:
    """``NovelAI Diffusion V4.5 4BDE2A90`` is a model name and a hash."""
    text = str(source or "").strip()
    if not text:
        return
    name, _, tail = text.rpartition(" ")
    if name and _is_hash(tail):
        families.put(result, "Model", name)
        families.put(result, "Model hash", tail)
    else:
        families.put(result, "Model", text)


def _is_hash(text: str) -> bool:
    return len(text) >= 8 and all(c in "0123456789abcdefABCDEF" for c in text)
