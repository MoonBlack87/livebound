"""Building the prompt and cleaning up what comes back.

The profile's system prompt owns every choice about content and voice. The user
turn assembled here contains only per-run data, profile numbers and the response
envelope that the application can parse.

Output cleaning is not cosmetic either. Models announce themselves ("Here is a
title:"), wrap things in code fences and quote strings, and every one of those
would end up in the published post.
"""

from __future__ import annotations

import base64
import io
import json
import math
import re
from pathlib import Path
from typing import Any

from PIL import Image

USER_TEMPLATE = (
    "PROFILE VALUES:\n"
    "Maximum title words: {title_max_words}\n"
    "Number of tags: {tag_count}\n\n"
    "{image_note}"
    "GENERATION PROMPTS:\n{prompts}\n\n"
    # The JSON object is the wire format. It stays in code because deleting it
    # from a profile would leave the application with no parseable suggestion.
    "Answer with one JSON object and nothing else, using this shape:\n"
    '{{"title":"...","description":"...","tags":["..."],"reason":"..."}}'
)

#: Strip a leading self-announcement. Each alternative carries its own colon -
#: requiring one after the group would need two.
_PREAMBLE_RE = re.compile(
    r"^(?:here (?:is|'s)[^:]{0,80}:|(?:description|prompt|answer|title|json)\s*:)\s*",
    re.IGNORECASE,
)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def clean_prompt(text: str, *, exclude_tags: list[str] | None = None) -> str:
    """Strip the machinery out of a generation prompt before the model sees it.

    LoRA tags, emphasis brackets, weights and bare hashes carry no meaning for a
    title and actively mislead - a model shown ``<lora:MoonTastic:0.8>`` will
    happily put "moontastic" in the tags.
    """
    text = re.sub(r"\bBREAK\b", " ", text or "", flags=re.IGNORECASE)
    text = re.sub(r"<[^>]*>", " ", text)          # <lora:...>, hypernets, embeddings
    for char in "()[]{}":
        text = text.replace(char, " ")            # emphasis brackets
    text = re.sub(r":\s*[0-9]+(\.[0-9]+)?", " ", text)          # :1.2 weights
    text = re.sub(r"\b[0-9a-f]{8,16}\b", " ", text, flags=re.IGNORECASE)  # bare hashes

    excluded = {tag.lower() for tag in (exclude_tags or [])}
    tags = [chunk.strip() for chunk in re.split(r"[,;]+", text) if chunk.strip()]
    tags = [tag for tag in tags if tag.lower() not in excluded]
    return ", ".join(tags)


def build_user_content(
    prompts: list[str],
    *,
    title_max_words: int,
    tag_count: int,
    hint: str | None = None,
    sheet: bool = False,
) -> str:
    """Assemble the data and response envelope for one suggestion."""
    joined = "\n\n".join(f"- {prompt}" for prompt in prompts if prompt)
    content = USER_TEMPLATE.format(
        title_max_words=title_max_words,
        tag_count=tag_count,
        prompts=(
            joined
            or "(No generation prompt is available; images are the only source material.)"
        ),
        image_note=(
            "IMAGE NOTE:\nThe first image is a contact sheet containing every picture "
            "in this post. Images after it are individual pictures shown at full size.\n\n"
            if sheet
            else ""
        ),
    )
    cleaned = (hint or "").strip()
    if cleaned:
        content += "\n\nADDITIONAL DIRECTION FROM THE AUTHOR:\n" + cleaned[:1000]
    return content


def _encode_jpeg(image, max_side: int) -> str:
    image = image.convert("RGB")
    image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=82)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def encode_images(paths: list[Path], *, max_side: int) -> list[str]:
    """Downscale for the vision encoder and base64-encode.

    These copies exist only for the model. They never touch the upload path,
    which sends a metadata-only copy at full resolution.
    """
    from PIL import Image as PILImage

    encoded: list[str] = []
    for path in paths:
        try:
            with PILImage.open(path) as image:
                encoded.append(_encode_jpeg(image, max_side))
        except Exception:
            continue
    return encoded


def build_contact_sheet(paths: list[Path], *, tile_side: int) -> str | None:
    """One image showing the whole post as a grid, or ``None``.

    A title is written for the *post*, not for one picture in it. Handing the
    model a single frame out of eight makes it describe that frame; handing it
    the set makes it describe what the set has in common - which is what a
    gallery title should say.

    **Every picture of the post is on the sheet**, each at ``tile_side``. The
    sheet grows with the post; the pictures do not shrink inside it. That is why
    the setting is the size of one picture and not a budget for the whole sheet -
    an earlier version divided a fixed width by the column count, so a five-image
    post showed each picture a third smaller than a four-image one, and a tenth
    picture was dropped without a word. With ``max_images`` at 1 the sheet is the
    only thing the model ever sees, so anything missing from it does not exist.

    The grid is as square as the count allows, and a short last row simply leaves
    empty cells - a model that can read the pictures can see that a cell is
    empty.

    Returns None for a single-image post, where a grid would just be the image
    with a border around it.
    """
    from PIL import Image as PILImage

    if len(paths) < 2:
        return None

    tiles = []
    for path in paths:
        try:
            with PILImage.open(path) as image:
                thumb = image.convert("RGB")
                thumb.thumbnail((tile_side, tile_side), PILImage.Resampling.LANCZOS)
                tiles.append(thumb.copy())
        except Exception:
            # One unreadable file must not cost the whole sheet.
            continue
    if len(tiles) < 2:
        return None

    columns = math.ceil(math.sqrt(len(tiles)))
    rows = -(-len(tiles) // columns)
    gap = 6
    sheet = PILImage.new(
        "RGB",
        (columns * tile_side + (columns + 1) * gap, rows * tile_side + (rows + 1) * gap),
        (18, 20, 26),
    )
    for index, thumb in enumerate(tiles):
        column, row = index % columns, index // columns
        x = gap + column * (tile_side + gap) + (tile_side - thumb.width) // 2
        y = gap + row * (tile_side + gap) + (tile_side - thumb.height) // 2
        sheet.paste(thumb, (x, y))

    buffer = io.BytesIO()
    sheet.save(buffer, "JPEG", quality=82)
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def parse_result(raw: str, *, title_max_words: int, tag_count: int) -> dict[str, Any]:
    """Read the model's JSON, then normalise every field it produced."""
    text = _THINK_RE.sub("", raw or "")
    text = _FENCE_RE.sub("", text).strip()
    text = _PREAMBLE_RE.sub("", text).strip()

    payload: dict[str, Any] | None = None
    start = text.find("{")
    if start != -1:
        try:
            payload, _ = json.JSONDecoder().raw_decode(text[start:])
        except ValueError:
            payload = None
    repaired = False
    if not isinstance(payload, dict):
        payload = _repaired(text[start:] if start != -1 else text)
        repaired = True
    if not isinstance(payload, dict):
        raise ValueError("The model's answer contains no readable JSON object.")

    result = {
        "title": _clean_title(payload.get("title"), title_max_words),
        "description": _clean_description(payload.get("description")),
        "tags": _clean_tags(payload.get("tags"), tag_count),
        "reason": _clean_line(payload.get("reason"), 300),
    }
    if repaired and not (result["title"] or result["description"] or result["tags"]):
        # `repair_json` answers a lone "{" with "{}", and an empty object is a
        # valid parse that yields nothing. Left alone, the run reports success,
        # writes no suggestion and gives the user no reason - worse than the
        # failure it replaced.
        raise ValueError("The model's answer contains no readable JSON object.")
    return result


def _repaired(text: str) -> dict[str, Any] | None:
    """Second attempt on an answer that is not valid JSON.

    Only reached once the plain parse has failed, so an answer that already
    works keeps exactly the path it has today. What comes back is not trusted
    either - it goes through the same cleaning as any other payload, because a
    repair can guess the shape wrong: one description came back as a list of
    strings rather than a string.
    """
    try:
        from json_repair import repair_json
    except ImportError:  # pragma: no cover - declared, but degrade rather than crash
        return None
    try:
        payload = json.loads(repair_json(text))
    except Exception:
        # A repair that cannot repair is a failed parse, not an error of its own.
        return None
    return payload if isinstance(payload, dict) else None

def _clean_title(value: Any, max_words: int) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    title = re.sub(r"\s+", " ", value).strip(" \"'.,:;")
    words = title.split()
    if len(words) > max_words:
        title = " ".join(words[:max_words])
    return title[:80].rstrip()


def _clean_description(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = _PREAMBLE_RE.sub("", value.strip()).strip("\"“”‘’'").strip()
    text = text.split("\n\n")[0]
    return re.sub(r"\s+", " ", text).strip()[:1500]


def _clean_tags(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        tag = re.sub(r"\s+", " ", item).strip(" #,.;:\"'").casefold()
        if 2 <= len(tag) <= 40 and tag not in seen:
            seen.add(tag)
            out.append(tag)
    return out[:limit]


def _clean_line(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()[:limit]
