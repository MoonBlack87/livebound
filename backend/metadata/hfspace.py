"""Read-only adapter for the Hugging Face Space demo writers.

Several Spaces save a flat JSON document into the ``parameters`` chunk that A1111
uses for text::

    {"prompt": "...", "negative_prompt": "...", "resolution": "1024 x 1024",
     "guidance_scale": 5.5, "num_inference_steps": 24, "seed": 443800072,
     "sampler": "DPM++ 2M Karras", "Model": "...", "Model hash": "..."}

It already spells two fields the way this application does, which is a hint that
whoever wrote it was looking at an A1111 infotext. The rest is mapped like every
other family. There is no vendor marker to detect on, so this adapter asks for
the shape instead - and it is registered after the families that do have one.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from . import families

FAMILY = "hf-space"
PARAMETERS_KEYWORD = "parameters"

_FIELD_NAMES: tuple[tuple[str, str], ...] = (
    ("num_inference_steps", "Steps"),
    ("sampler", "Sampler"),
    ("guidance_scale", "CFG scale"),
    ("seed", "Seed"),
    ("Model", "Model"),
    ("Model hash", "Model hash"),
)

#: What this shape must have before it is claimed. Two of them are enough to
#: tell it from another family's document and from a prompt someone typed.
_REQUIRED = ("prompt", "num_inference_steps")


def detects(chunks: Mapping[str, str], user_comment: str | None) -> bool:
    """Whether the container metadata has this shape."""
    document = families.first_document(chunks.get(PARAMETERS_KEYWORD))
    return isinstance(document, dict) and all(key in document for key in _REQUIRED)


def raw_text(chunks: Mapping[str, str], user_comment: str | None) -> str | None:
    """Read the source text from the container location used by this family."""
    return chunks.get(PARAMETERS_KEYWORD)


def read(chunks: Mapping[str, str], user_comment: str | None) -> dict[str, Any]:
    """Read this family's container metadata into the common parsed shape."""
    result = families.empty_result()
    document = families.first_document(chunks.get(PARAMETERS_KEYWORD))
    if not isinstance(document, dict):
        return result

    result["prompt"] = str(document.get("prompt") or "")
    negative = document.get("negative_prompt")
    result["negative_prompt"] = str(negative).strip() or None if negative else None

    families.put_mapped(result, document, _FIELD_NAMES)
    width, _, height = str(document.get("resolution") or "").partition("x")
    families.put_size(result, width.strip(), height.strip())
    return result
