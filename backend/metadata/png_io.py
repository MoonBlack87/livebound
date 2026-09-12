"""Reading metadata out of an image file. Reading only - nothing here writes.

That restriction is load-bearing. The source is the image's local identity and
must remain exactly what the generator wrote. Upload metadata belongs only on a
short-lived materialised copy; this module therefore has no write path.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from PIL import Image

from . import (
    civitai_generated,
    comfyui,
    fooocus,
    fooocusplus,
    hfspace,
    infotext,
    invokeai,
    novelai,
    ruinedfooocus,
    swarmui,
    tensorart,
)

_EXIF_USER_COMMENT = 0x9286
_EXIF_MODEL = 0x0110
#: Where the tag belongs; some generators write it into IFD0 instead.
_EXIF_IFD = 0x8769

UNKNOWN_METADATA_FAMILY = "unknown"
UNKNOWN_METADATA_WARNING = "Unknown metadata format; metadata was not read."
UNREADABLE_METADATA_WARNING = "Metadata could not be read."

# This is deliberately a tuple of concrete modules, not an interface hierarchy.
# A further adapter adds its detector and reader here without changing the
# scanner or the container-reading path.
#: Order is behaviour: the specific families are asked before the general
#: one, and the first match wins. Most of them read the same `parameters`
#: chunk, so a family that answers "mine" too eagerly takes another's
#: images with it. FooocusPlus is asked before Fooocus for that reason: it
#: is a fork, and it kept the parent's marker alongside its own.
_ADAPTERS: tuple[ModuleType, ...] = (
    novelai,
    invokeai,
    tensorart,
    swarmui,
    ruinedfooocus,
    fooocusplus,
    fooocus,
    hfspace,
    infotext,
    civitai_generated,
    comfyui,
)


@dataclass
class ImageFacts:
    width: int
    height: int
    content_type: str
    infotext: str | None = None
    chunks: dict[str, str] = field(default_factory=dict)
    user_comment: str | None = None
    metadata_family: str = UNKNOWN_METADATA_FAMILY

    @property
    def has_metadata(self) -> bool:
        """Whether the file carries metadata that a generator may have written."""
        return bool(self.chunks) or self.user_comment is not None


def _detect_adapter(
    chunks: dict[str, str], user_comment: str | None
) -> ModuleType | None:
    return next(
        (adapter for adapter in _ADAPTERS if adapter.detects(chunks, user_comment)),
        None,
    )


def detect_metadata_family(chunks: dict[str, str], user_comment: str | None) -> str:
    """Name the supported generator family, or honestly report ``unknown``."""
    adapter = _detect_adapter(chunks, user_comment)
    return str(adapter.FAMILY) if adapter is not None else UNKNOWN_METADATA_FAMILY


def read_metadata(facts: ImageFacts) -> dict[str, Any]:
    """Dispatch already-extracted container metadata to its family adapter."""
    adapter = next(
        (item for item in _ADAPTERS if facts.metadata_family == item.FAMILY),
        None,
    )
    if adapter is None:
        parsed = infotext.parse(None)
        if facts.has_metadata:
            parsed["warnings"].append(UNKNOWN_METADATA_WARNING)
        return parsed

    try:
        return adapter.read(facts.chunks, facts.user_comment)
    except Exception:
        # One metadata reader's failure must not discard the otherwise readable
        # image or abort the scan containing it.
        parsed = infotext.parse(None)
        parsed["warnings"].append(UNREADABLE_METADATA_WARNING)
        return parsed


def read(path: Path, image: Image.Image | None = None) -> ImageFacts | None:
    """Dimensions plus whatever metadata the file carries. ``None`` if unreadable.

    An unreadable file must never abort a scan, so failures come back as None
    rather than as an exception.
    """
    def extract(source: Image.Image) -> ImageFacts:
        width, height = source.size
        fmt = source.format or ""
        chunks = {
            str(k): str(v) for k, v in (getattr(source, "text", {}) or {}).items()
        }
        model_prompt = _from_exif_model_prompt(source)
        if model_prompt is not None:
            # This is not a PNG text chunk. Keeping its distinct key preserves
            # where it came from while letting the ordinary adapter tuple read it.
            chunks[comfyui.EXIF_PROMPT_KEY] = model_prompt
        user_comment = _from_exif(source)
        family = detect_metadata_family(chunks, user_comment)
        adapter = next((item for item in _ADAPTERS if family == item.FAMILY), None)
        # The raw text is the container's, not the family's. A file whose
        # metadata no adapter recognises - a ComfyUI graph today - still carries
        # text somebody wrote, and dropping it would lose the only copy: the
        # library stores this, and a parser added later reads it from there
        # rather than from a file that may have moved since.
        raw_infotext = (
            adapter.raw_text(chunks, user_comment)
            if adapter is not None
            else chunks.get(infotext.PARAMETERS_KEYWORD) or user_comment
        )
        # What the decoder read beats what the name claims. A file may be named
        # `.jpeg` and hold PNG bytes - a download that was renamed is enough -
        # and this type is stored, served, and announced to CivitAI on upload,
        # where a pre-signed PUT keeps whatever Content-Type it was given.
        content_type = (
            f"image/{fmt.lower()}"
            if fmt
            else mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        )
        return ImageFacts(
            width=width,
            height=height,
            content_type=content_type,
            infotext=raw_infotext,
            chunks=chunks,
            user_comment=user_comment,
            metadata_family=family,
        )

    try:
        if image is not None:
            return extract(image)
        with Image.open(path) as opened:  # lazy: no pixel decode
            return extract(opened)
    except Exception:
        # One unreadable image must not abort the scan containing it.
        return None


def _from_exif(image: Image.Image) -> str | None:
    """JPEG/WebP put the infotext in EXIF UserComment instead of a text chunk.

    Looked for in both places it turns up: the Exif sub-IFD, where the tag
    belongs, and IFD0, where some generators put it anyway.
    """
    try:
        exif = image.getexif()
    except Exception:
        return None
    if not exif:
        return None
    raw = exif.get(_EXIF_USER_COMMENT)
    if raw is None:
        try:
            raw = exif.get_ifd(_EXIF_IFD).get(_EXIF_USER_COMMENT)
        except Exception:
            raw = None
    return _decode_exif_text(raw)


def _from_exif_model_prompt(image: Image.Image) -> str | None:
    """Read the ``prompt:`` graph some WebP save nodes put in EXIF Model."""
    try:
        raw = image.getexif().get(_EXIF_MODEL)
    except Exception:
        return None
    text = _decode_exif_text(raw)
    return text.removeprefix("prompt:") if text and text.startswith("prompt:") else None


def _decode_exif_text(raw: Any) -> str | None:
    if isinstance(raw, bytes):
        for prefix, encoding in (
            (b"UNICODE\x00", "utf-16-be"),
            (b"ASCII\x00\x00\x00", "utf-8"),
        ):
            if raw.startswith(prefix):
                try:
                    return raw[len(prefix) :].decode(encoding, errors="replace")
                except Exception:
                    return None
        try:
            return raw.decode("utf-8", errors="replace")
        except Exception:
            return None
    return raw if isinstance(raw, str) else None
