"""Read-only adapter for A1111-family "infotext" metadata.

The text lives in a single PNG ``tEXt`` chunk with keyword ``parameters`` and has
three parts::

    <prompt>
    Negative prompt: <negative prompt>
    Steps: 24, Sampler: DPM++ 2M SDE, ..., Civitai resources: [{...},{...}]

The trailing parameter line **cannot** be split on commas: ``Civitai resources``
holds a JSON array, and ``Hires prompt`` / ``Lora hashes`` / ``TI hashes`` hold
quoted strings containing their own commas. So the line is walked left to right,
and whenever a value starts with ``"``, ``[`` or ``{`` it is handed to
``json.JSONDecoder.raw_decode``, which consumes exactly one JSON value - nested
brackets and escaped quotes included - and reports where it ended.

Serialization lives in :mod:`.compose`; container-specific writing lives in
:mod:`.write`. Both target a temporary upload copy and never this parser's source
file.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

FAMILY = "a1111"
PARAMETERS_KEYWORD = "parameters"
NEGATIVE_PROMPT_PREFIX = "Negative prompt: "

#: ``(separator)(Key):`` - the key charset covers every real field name seen in
#: the wild ("Model hash", "Schedule type", "Hires CFG Scale", ...).
_KEY_RE = re.compile(r"(?P<prefix>\s*(?:,\s*)?)(?P<key>[A-Za-z][A-Za-z0-9 _./\-]*?):[ \t]*")

#: Boundary between a plain scalar value and the next key.
_NEXT_KEY_RE = re.compile(r",\s*[A-Za-z][A-Za-z0-9 _./\-]*?:[ \t]")

#: How many ``Key: value`` pairs a line needs before it counts as the parameter
#: line rather than as part of the prompt. The heuristic A1111 itself uses.
_MIN_PARAM_PAIRS = 3

_decoder = json.JSONDecoder()

_LORA_TAG_RE = re.compile(r"<lora:([^:>]+):([-0-9.]+)\s*>")
_INT_KEYS = {"Steps", "Seed", "Clip skip", "ENSD", "Hires steps"}
_FLOAT_KEYS = {"CFG scale", "Denoising strength", "Hires CFG Scale", "Hires upscale"}


@dataclass(frozen=True)
class ParameterEntry:
    """One parameter together with the generator's original spelling."""

    key: str
    value: Any
    raw: str
    prefix: str
    source: str | None


def detects(chunks: Mapping[str, str], user_comment: str | None) -> bool:
    """Whether the container metadata has the A1111-family shape.

    A1111 writes plain text. Three families share this chunk, and the other two
    write a JSON document into it - so a JSON object here is never this family,
    and before that was checked their images were parsed as infotext with the
    whole document becoming the prompt, silently and without a warning.

    Everything else stays ours, including text with no parameter line at all: a
    bare prompt is still a prompt, and refusing it would lose one we can read.
    A prompt that opens with ``{`` is not JSON either - dynamic prompts look
    exactly like that (``{red|blue} car``) - so the test is a parse, not a
    first character. Measured on a real library of 4190 images: 4133 of 4142
    infotexts carry ``Steps: ``, and the nine that do not are ComfyUI graphs.
    """
    text = raw_text(chunks, user_comment)
    return bool(text) and not _is_json_document(text)


def _is_json_document(text: str) -> bool:
    """Whether the text is a JSON object, which another family wrote."""
    if not text.lstrip().startswith("{"):
        return False
    try:
        return isinstance(json.loads(text), dict)
    except (json.JSONDecodeError, RecursionError):
        return False


def raw_text(chunks: Mapping[str, str], user_comment: str | None) -> str | None:
    """Read the source text from the container location used by this family."""
    if PARAMETERS_KEYWORD in chunks:
        return chunks[PARAMETERS_KEYWORD]
    return user_comment


def read(chunks: Mapping[str, str], user_comment: str | None) -> dict[str, Any]:
    """Read this family's container metadata into the common parsed shape."""
    return parse(raw_text(chunks, user_comment))


def _scan_scalar_end(text: str, start: int) -> int:
    match = _NEXT_KEY_RE.search(text, start)
    return match.start() if match else len(text)


def _scan_lenient_end(text: str, start: int) -> int:
    """Fallback for a quoted value whose contents are not valid JSON."""
    pos = start + 1
    while True:
        idx = text.find('"', pos)
        if idx == -1:
            return _scan_scalar_end(text, start)
        after = text[idx + 1 :]
        if not after or _NEXT_KEY_RE.match(after) or after.strip() == "":
            return idx + 1
        pos = idx + 1


def _decode(key: str, raw: str) -> Any:
    if not raw:
        return raw
    if raw[0] in '"[{':
        try:
            return _decoder.decode(raw)
        except ValueError:
            return raw
    if key in _INT_KEYS:
        try:
            return int(raw)
        except ValueError:
            return raw
    if key in _FLOAT_KEYS:
        try:
            return float(raw)
        except ValueError:
            return raw
    return raw


def parse_parameter_line(line: str) -> tuple[list[ParameterEntry], list[str]]:
    """Parse a parameter line without discarding its original field segments."""
    entries: list[ParameterEntry] = []
    warnings: list[str] = []
    pos = 0
    while pos < len(line):
        match = _KEY_RE.match(line, pos)
        if not match:
            remainder = line[pos:].strip()
            if remainder:
                warnings.append(f"Unreadable remainder: {remainder[:80]!r}")
            break

        key = match.group("key")
        vstart = match.end()
        if vstart < len(line) and line[vstart] in '"[{':
            try:
                _, endpos = _decoder.raw_decode(line, vstart)
            except (json.JSONDecodeError, RecursionError):
                # RecursionError too: a deeply nested value in a downloaded PNG
                # otherwise escapes parse() and turns opening the drawer into a
                # 500. Reading leniently is exactly the right answer for both.
                endpos = _scan_lenient_end(line, vstart)
                warnings.append(f"{key}: not valid JSON, read leniently")
        else:
            endpos = _scan_scalar_end(line, vstart)

        raw = line[vstart:endpos].strip()
        entries.append(
            ParameterEntry(
                key=key,
                value=_decode(key, raw),
                raw=raw,
                prefix=match.group("prefix"),
                source=line[match.start("key") : endpos],
            )
        )
        pos = endpos
    return entries, warnings


def _parse_param_line(line: str) -> tuple[list[tuple[str, Any, str]], list[str]]:
    entries, warnings = parse_parameter_line(line)
    return [(entry.key, entry.value, entry.raw) for entry in entries], warnings


def _looks_like_param_line(line: str) -> bool:
    entries, _ = _parse_param_line(line)
    return len(entries) >= _MIN_PARAM_PAIRS


def _find_negative_prompt(body: str) -> int | None:
    if body.startswith(NEGATIVE_PROMPT_PREFIX):
        return 0
    idx = body.find("\n" + NEGATIVE_PROMPT_PREFIX)
    return None if idx == -1 else idx + 1


def parse(text: str | None) -> dict[str, Any]:
    """Parse infotext into ``{prompt, negative_prompt, fields, field_order, warnings}``."""
    result: dict[str, Any] = {
        "prompt": "",
        "negative_prompt": None,
        "fields": {},
        "field_order": [],
        "warnings": [],
    }
    stripped = (text or "").rstrip("\n")
    if not stripped:
        return result

    lines = stripped.split("\n")
    if len(lines) > 1 and _looks_like_param_line(lines[-1]):
        entries, warnings = _parse_param_line(lines[-1])
        body_lines = lines[:-1]
    elif len(lines) == 1 and _looks_like_param_line(lines[0]):
        # a bare parameter line with no prompt at all
        entries, warnings = _parse_param_line(lines[0])
        body_lines = []
    else:
        entries, warnings, body_lines = [], [], lines

    body = "\n".join(body_lines)
    neg_idx = _find_negative_prompt(body)
    if neg_idx is None:
        result["prompt"] = body
    else:
        result["prompt"] = body[:neg_idx].rstrip("\n")
        result["negative_prompt"] = body[neg_idx + len(NEGATIVE_PROMPT_PREFIX) :]

    result["fields"] = {key: value for key, value, _ in entries}
    result["field_order"] = [key for key, _, _ in entries]
    result["warnings"] = warnings
    return result


def lora_tags(prompt: str | None) -> list[tuple[str, float]]:
    """``<lora:name:weight>`` references, in order of appearance."""
    if not prompt:
        return []
    out: list[tuple[str, float]] = []
    for match in _LORA_TAG_RE.finditer(prompt):
        try:
            weight = float(match.group(2))
        except ValueError:
            weight = 1.0
        out.append((match.group(1).strip(), weight))
    return out


def parse_hash_map(value: Any) -> dict[str, str]:
    """Read ``Lora hashes`` / ``TI hashes``: ``"name: hash, name: hash"``."""
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if not isinstance(value, str) or not value.strip():
        return {}
    out: dict[str, str] = {}
    for chunk in value.split(","):
        name, _, digest = chunk.partition(":")
        name, digest = name.strip(), digest.strip()
        if name and digest:
            out[name] = digest
    return out
