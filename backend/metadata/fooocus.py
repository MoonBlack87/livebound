"""Read-only adapter for Fooocus metadata.

Fooocus can save its parameters two ways, and the setting is the user's::

    a1111    the plain infotext everyone knows, read by `infotext.py`
    fooocus  a JSON document, read here

Both land in the same place - the ``parameters`` chunk of a PNG, the EXIF
UserComment of a JPEG or WebP - so the two schemes are told apart by their
content, never by where they sit.

The JSON scheme names its own facts::

    {"prompt": "A sunflower field", "full_prompt": ["cinematic still …", "…"],
     "base_model": "juggernautXL_v8Rundiffusion", "base_model_hash": "aeb7e9e689",
     "guidance_scale": 4, "resolution": "(512, 512)", "seed": "127589946317439009",
     "loras": [["sd_xl_offset_example-lora_1.0", 0.1, "4852686128"]],
     "metadata_scheme": "fooocus", "version": "Fooocus v2.5.5"}

**The names used here are Fooocus's own.** Saving the same image in the a1111
scheme produces ``Sharpness``, ``ADM Guidance``, ``Performance``, ``VAE``,
``Raw prompt``, ``Lora hashes`` and ``Lora weights`` - so those are what the JSON
is mapped onto, and one save setting does not decide under which spelling a fact
arrives. Only ``scheduler`` is renamed, to the ``Schedule type`` this application
already carries for that fact everywhere else.

**The prompt is the expanded one**, ``full_prompt`` joined, with the user's own
words kept in ``Raw prompt``. That is what Fooocus itself writes as the prompt in
the a1111 scheme, and what a viewer on CivitAI sees for such an image today; the
alternative would publish the same picture differently depending on how it
happened to be saved.

Not read here: the ``log.html`` private log Fooocus writes beside the images, and
the pre-2.2 scheme that only ever appeared in it. Neither is embedded in an image
file, and this application reads images.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from . import families

FAMILY = "fooocus"
PARAMETERS_KEYWORD = "parameters"

#: A JSON document, not the a1111 text, and not the fork below it. `infotext.py`
#: takes the plain text; `fooocusplus.py` is asked before this and takes its own.
_MARKER_RE = re.compile(r'"metadata_scheme"\s*:|"version"\s*:\s*"Fooocus\s')


@dataclass(frozen=True)
class Scheme:
    """Which key a fork of Fooocus writes for each fact.

    FooocusPlus keeps the same document with different spellings, so it supplies
    its own instance rather than a second copy of the reader below.
    """

    prompt: str
    negative_prompt: str
    full_prompt: str
    full_negative_prompt: str
    resolution: str
    loras: str
    lora_combined: str
    refiner_model: str
    refiner_switch: str
    base_model: str
    fields: tuple[tuple[str, str], ...]


#: Fooocus's own name for a fact, and the one this application records. They are
#: the same wherever Fooocus has one, which is nearly everywhere.
_FIELDS: tuple[tuple[str, str], ...] = (
    ("steps", "Steps"),
    ("sampler", "Sampler"),
    ("scheduler", "Schedule type"),
    ("guidance_scale", "CFG scale"),
    ("seed", "Seed"),
    ("sharpness", "Sharpness"),
    ("adm_guidance", "ADM Guidance"),
    ("base_model_hash", "Model hash"),
    ("performance", "Performance"),
    ("vae", "VAE"),
    ("clip_skip", "Clip skip"),
    ("version", "Version"),
    ("prompt", "Raw prompt"),
    ("negative_prompt", "Raw negative prompt"),
)

SCHEME = Scheme(
    prompt="prompt",
    negative_prompt="negative_prompt",
    full_prompt="full_prompt",
    full_negative_prompt="full_negative_prompt",
    resolution="resolution",
    loras="loras",
    lora_combined="lora_combined_",
    refiner_model="refiner_model",
    refiner_switch="refiner_switch",
    base_model="base_model",
    fields=_FIELDS,
)

#: What Fooocus writes for a refiner that was not used. The switch that goes with
#: it is then a default nobody chose, so neither is recorded.
_UNUSED = "None"

_RESOLUTION_RE = re.compile(r"(\d+)\s*\D+\s*(\d+)")


def detects(chunks: Mapping[str, str], user_comment: str | None) -> bool:
    """Whether the container metadata has Fooocus's JSON shape."""
    return bool(_MARKER_RE.search(raw_text(chunks, user_comment) or ""))


def raw_text(chunks: Mapping[str, str], user_comment: str | None) -> str | None:
    """Read the source text from the container locations used by this family."""
    return chunks.get(PARAMETERS_KEYWORD) or user_comment


def read(chunks: Mapping[str, str], user_comment: str | None) -> dict[str, Any]:
    """Read this family's container metadata into the common parsed shape."""
    return parse(raw_text(chunks, user_comment))


def parse(text: str | None) -> dict[str, Any]:
    """Parse a Fooocus JSON document into the common parsed shape."""
    return parse_scheme(text, SCHEME, FAMILY)


def parse_scheme(text: str | None, scheme: Scheme, family: str) -> dict[str, Any]:
    """Parse one fork's document. Shared, because only the spellings differ."""
    result = families.empty_result()
    if not text:
        return result

    document = families.first_document(text)
    if not isinstance(document, dict):
        result["warnings"].append(f"{family} metadata is not an object")
        return result

    result["prompt"] = _joined(document.get(scheme.full_prompt)) or str(
        document.get(scheme.prompt) or ""
    )
    negative = _joined(document.get(scheme.full_negative_prompt)) or str(
        document.get(scheme.negative_prompt) or ""
    )
    result["negative_prompt"] = negative or None

    families.put_mapped(result, document, scheme.fields)
    families.put(result, "Model", _model_name(document.get(scheme.base_model)))
    _read_resolution(document.get(scheme.resolution), result)
    if str(document.get(scheme.refiner_model) or _UNUSED) != _UNUSED:
        families.put(result, "Refiner model", document.get(scheme.refiner_model))
        families.put(result, "Refiner switch", document.get(scheme.refiner_switch))
    _read_loras(document, scheme, result)
    return result


def _joined(value: Any) -> str:
    """``full_prompt`` is the styled prompt and the expansion, in two parts.

    Joining them with ", " is what Fooocus itself writes as the prompt in the
    a1111 scheme. A part the next one already contains word for word is dropped
    first: FooocusPlus with no styles puts the bare prompt in the first part and
    an expansion of it in the second, and joining those says the subject twice.
    """
    if not isinstance(value, list):
        return str(value or "")
    parts = [str(part).strip() for part in value if str(part).strip()]
    return ", ".join(
        part
        for index, part in enumerate(parts)
        if not any(part in later for later in parts[index + 1 :])
    )


def _read_resolution(value: Any, result: dict[str, Any]) -> None:
    """``"(512, 512)"`` is a Python tuple that was printed, not a JSON pair."""
    if isinstance(value, (list, tuple)) and len(value) == 2:
        families.put_size(result, value[0], value[1])
        return
    found = _RESOLUTION_RE.search(str(value or ""))
    if found:
        families.put_size(result, found.group(1), found.group(2))


def _read_loras(document: dict[str, Any], scheme: Scheme, result: dict[str, Any]) -> None:
    """``[[name, weight, hash]]``, or the numbered ``name : weight`` before it.

    The hash is the ten-character AutoV2 an infotext carries, which is what
    :mod:`.resources` resolves a local file by. Fooocus v2.2 wrote only the
    numbered strings, and those have no hash at all: such an image states the
    weight of a LoRA that cannot be identified, and that is the honest result.
    """
    hashes: dict[str, str] = {}
    weights: dict[str, Any] = {}

    entries = document.get(scheme.loras)
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, (list, tuple)) or len(entry) < 2:
                continue
            name = _model_name(entry[0])
            if not name:
                continue
            weights[name] = entry[1]
            digest = str(entry[2]).strip() if len(entry) > 2 else ""
            if digest:
                hashes[name] = digest

    for key, value in document.items():
        if not str(key).startswith(scheme.lora_combined) or not isinstance(value, str):
            continue
        name, separator, weight = value.rpartition(" : ")
        name = _model_name(name if separator else value)
        if name and name not in weights:
            weights[name] = weight.strip() if separator else None

    if hashes:
        families.put(
            result, "Lora hashes", ", ".join(f"{n}: {d}" for n, d in hashes.items())
        )
    stated = {n: w for n, w in weights.items() if w not in (None, "")}
    if stated:
        families.put(
            result, "Lora weights", ", ".join(f"{n}: {w}" for n, w in stated.items())
        )


#: Written with the extension in some places and without it in others. Only
#: these are removed: a LoRA called `sd_xl_offset_example-lora_1.0` must keep
#: everything after its last dot.
_MODEL_SUFFIXES = (".safetensors", ".ckpt", ".pt", ".bin")


def _model_name(value: Any) -> str:
    """A file name here, a bare name elsewhere; the folder is one machine's."""
    name = re.split(r"[\\/]", str(value or ""))[-1].strip()
    for suffix in _MODEL_SUFFIXES:
        if name.lower().endswith(suffix):
            return name[: -len(suffix)]
    return name
