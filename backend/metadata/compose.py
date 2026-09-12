"""Rendering an edited infotext back out.

The counterpart to :mod:`backend.metadata.infotext`, and deliberately built on
the *raw* text rather than on the parsed document: an untouched field is copied
across exactly as the generator wrote it, quoting, spacing and all. Re-rendering
everything from decoded values would change fields nobody asked to change, and
CivitAI reads what it is given.

The round-trip contract, pinned by a test: composing an empty edit over an
infotext reproduces it byte for byte. A changed structured value is rendered
again; its safe JSON spelling is not promised to match the generator's original
spacing.
"""

from __future__ import annotations

import json
from typing import Any

from .infotext import (
    NEGATIVE_PROMPT_PREFIX,
    ParameterEntry,
    _find_negative_prompt,
    _looks_like_param_line,
    _parse_param_line,
)


def apply_edit(raw_infotext: str | None, edit: dict[str, Any] | None) -> str:
    """The infotext this image should be uploaded with.

    ``edit`` is what :mod:`backend.store.edits` holds: a partial draft, the list
    of fields the user set, and the list they removed. Anything not named there
    comes through untouched.
    """
    text = (raw_infotext or "").rstrip("\n")
    if not edit or not (edit.get("touched") or edit.get("deleted")):
        return text

    draft = edit.get("draft") or {}
    touched = set(edit.get("touched") or [])
    deleted = set(edit.get("deleted") or [])

    body, param_line = _split(text)
    prompt, negative = _split_body(body)

    if "prompt" in touched:
        prompt = draft.get("prompt") or ""
    if "negative_prompt" in touched:
        negative = draft.get("negative_prompt")
    if "negative_prompt" in deleted:
        negative = None

    fields = draft.get("fields") or {}
    param_line = _compose_params(param_line, fields, touched, deleted)

    parts = [prompt]
    if negative is not None:
        parts.append(NEGATIVE_PROMPT_PREFIX + negative)
    if param_line:
        parts.append(param_line)
    return "\n".join(parts)


def _split(text: str) -> tuple[str, str]:
    """The prompt block and the parameter line, exactly as :func:`infotext.parse` cuts them."""
    if not text:
        return "", ""
    lines = text.split("\n")
    if _looks_like_param_line(lines[-1]):
        return "\n".join(lines[:-1]), lines[-1]
    return text, ""


def _split_body(body: str) -> tuple[str, str | None]:
    index = _find_negative_prompt(body)
    if index is None:
        return body, None
    return body[:index].rstrip("\n"), body[index + len(NEGATIVE_PROMPT_PREFIX) :]


def _compose_params(
    line: str, values: dict[str, Any], touched: set[str], deleted: set[str]
) -> str:
    entries, _ = _parse_param_line(line)
    seen = {key for key, _, _ in entries}

    rendered: list[str] = []
    for key, _, raw in entries:
        if key in deleted:
            continue
        rendered.append(f"{key}: {render(values[key]) if key in touched else raw}")

    # Fields the user added are appended: an infotext has no fixed order beyond
    # what the generator wrote, and inserting into the middle would move
    # everything after it for no reason.
    for key, value in values.items():
        if key in seen or key in deleted or key not in touched:
            continue
        rendered.append(f"{key}: {render(value)}")

    return ", ".join(rendered)


def render_infotext(
    prompt: str, negative_prompt: str | None, parameters: list[ParameterEntry]
) -> str:
    """Render a complete A1111 block from effective fields and parameters."""
    body = [prompt] if prompt else []
    if negative_prompt is not None:
        body.append(NEGATIVE_PROMPT_PREFIX + negative_prompt)

    line = ""
    for index, parameter in enumerate(parameters):
        source = parameter.source
        if source is None:
            source = f"{parameter.key}: {render(parameter.value)}"
        prefix = "" if index == 0 else parameter.prefix if "," in parameter.prefix else ", "
        line += prefix + source
    if line:
        body.append(line)
    return "\n".join(body)


def render(value: Any) -> str:
    """A value as it belongs in a parameter line.

    A string that would otherwise swallow the next key - one containing a comma -
    is quoted as JSON, which is how the generators write ``Lora hashes`` and
    friends and how the parser reads them back.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (dict, list)):
        # PNG tEXt is Latin-1. Generator JSON escapes non-ASCII values too; keep
        # those escapes so a changed blob remains writable. The separators are
        # intentionally ordinary JSON and may differ from the generator's spelling.
        return json.dumps(value, ensure_ascii=True, separators=(", ", ": "))
    text = str(value)
    if "," in text or text.startswith(('"', "[", "{")):
        return json.dumps(text, ensure_ascii=False)
    return text
