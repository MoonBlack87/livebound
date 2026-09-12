"""Read-only adapter for FooocusPlus metadata.

FooocusPlus is a fork of Fooocus and keeps its document, with two differences
that matter for reading it: the keys are written in title case, and in a PNG it
goes into a ``Comment`` chunk rather than into ``parameters``::

    {"Prompt": "A sunflower field", "Base Model": "elsewhereXL_v10",
     "Base Model Hash": "79fd29ab43", "Guidance Scale": 4.5,
     "Resolution": "(1024, 1024)", "Backend Engine": "SDXL-Fooocus",
     "User": "FooocusPlus", "Metadata Scheme": "Fooocus",
     "Version": "FooocusPlus 1.0.0"}

Detection therefore cannot go by the chunk: ``Comment`` is also what an image
editor writes into a picture that has no generation data at all. It goes by what
only this fork puts in the document - its own name, and the engine it ran.

Because everything else is the parent's, this adapter is a table of spellings on
top of :mod:`.fooocus` rather than a second reader. A fork that renamed one key
would otherwise quietly get a second answer for every fact it did not rename.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from . import fooocus

FAMILY = "fooocusplus"

#: Where this fork writes, in the order it is looked for: its own PNG chunk, the
#: parent's, then the EXIF field a JPEG and a WebP have instead.
_CHUNKS = ("Comment", "parameters")

#: Only this fork names itself in the document. Both spellings of the engine key
#: occur - the title-case scheme is what the released version writes.
_MARKER_RE = re.compile(
    r'"[Bb]ackend[ _][Ee]ngine"\s*:|"[Vv]ersion"\s*:\s*"FooocusPlus|'
    r'"[Uu]ser"\s*:\s*"FooocusPlus'
)

_FIELDS: tuple[tuple[str, str], ...] = (
    ("Steps", "Steps"),
    ("Sampler", "Sampler"),
    ("Scheduler", "Schedule type"),
    ("Guidance Scale", "CFG scale"),
    ("Seed", "Seed"),
    ("Sharpness", "Sharpness"),
    ("ADM Guidance", "ADM Guidance"),
    ("Base Model Hash", "Model hash"),
    ("Performance", "Performance"),
    ("VAE", "VAE"),
    ("CLIP Skip", "Clip skip"),
    ("Backend Engine", "Backend engine"),
    ("Version", "Version"),
    ("Prompt", "Raw prompt"),
    ("Negative Prompt", "Raw negative prompt"),
)

SCHEME = fooocus.Scheme(
    prompt="Prompt",
    negative_prompt="Negative Prompt",
    full_prompt="Full Prompt",
    full_negative_prompt="Full Negative Prompt",
    resolution="Resolution",
    loras="LoRAs",
    lora_combined="LoRA ",
    refiner_model="Refiner Model",
    refiner_switch="Refiner Switch",
    base_model="Base Model",
    fields=_FIELDS,
)


def detects(chunks: Mapping[str, str], user_comment: str | None) -> bool:
    """Whether the container metadata has FooocusPlus's shape."""
    return bool(_MARKER_RE.search(raw_text(chunks, user_comment) or ""))


def raw_text(chunks: Mapping[str, str], user_comment: str | None) -> str | None:
    """Read the source text from the container locations used by this family."""
    for keyword in _CHUNKS:
        if chunks.get(keyword):
            return chunks[keyword]
    return user_comment


def read(chunks: Mapping[str, str], user_comment: str | None) -> dict[str, Any]:
    """Read this family's container metadata into the common parsed shape."""
    return parse(raw_text(chunks, user_comment))


def parse(text: str | None) -> dict[str, Any]:
    """Parse a FooocusPlus JSON document into the common parsed shape."""
    return fooocus.parse_scheme(text, SCHEME, FAMILY)
