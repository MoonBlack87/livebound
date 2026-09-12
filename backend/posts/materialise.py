"""Choose the bytes an image contributes to a push.

The library file is the image's identity and is never written. Every upload is
a short-lived copy carrying the metadata that applies at that moment.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

from .. import db
from ..metadata import (
    civitai_generated,
    comfyui,
    compose,
    families,
    infotext,
    png_io,
    prompt_tools,
    resource_edits,
    swarmui,
    write,
)
from ..metadata import upload_document as document_renderer
from ..store import edits

EXCLUDED_FIELDS_SETTING = "metadata_excluded_fields"
PROMPT_EXCLUSIONS_SETTING = "prompt_exclusions"
_STORED_EDIT = object()


def upload_infotext(
    image: dict[str, Any], *, edit_document: dict[str, Any] | None | object = _STORED_EDIT
) -> str:
    """The effective, projected infotext represented by an upload."""
    projection = _upload_projection(image, edit_document)
    rendered = document_renderer.render_infotext(projection)
    return _native_upload_text(
        image,
        rendered,
        projection.resources,
        projection.source_resources,
        projection.source_generator,
    )


def _upload_projection(
    image: dict[str, Any], edit_document: dict[str, Any] | None | object = _STORED_EDIT
) -> document_renderer.Document:
    """Build the single model behind the upload text and CivitAI ``meta``."""
    image_id = image.get("image_id")
    if image_id is None:
        image_id = image.get("id")
    raw = image.get("raw_infotext") or ""
    source_family = _source_family(raw)
    source = _source_infotext(image, raw)
    if edit_document is _STORED_EDIT:
        edit = edits.get(image_id) if image_id is not None else None
    else:
        edit = edit_document
    effective = compose.apply_edit(source, edit) if edit else source
    # MET-10: apply exclusions before projection so a field cannot retain the
    # resource that was derived from it.
    effective = strip_excluded_fields(effective)
    # MET-23: prompt exclusions change the rendered prompts but not the rows
    # selected from the pre-exclusion effective metadata.
    rendered = apply_prompt_exclusions(effective)
    projection = _project_upload_document(image, source, effective, rendered)
    if (
        source_family == comfyui.FAMILY
        and _native_png(image)
        and not _comfyui_workflow_replaced(image, projection)
    ):
        return projection
    return replace(projection, source_generator=families.origin_label(source_family))


def _project_upload_document(
    image: dict[str, Any],
    source: str,
    effective: str,
    rendered: str,
) -> document_renderer.Document:
    """Build the document that the upload will state before provenance is added."""
    image_id = image.get("image_id", image.get("id"))
    source_parsed = infotext.parse(source)
    if not source_parsed.get("fields") and _parsed_reading(image).get("fields"):
        source_parsed = _parsed_reading(image)
    effective_parsed = infotext.parse(rendered)
    if rendered == source:
        effective_parsed = source_parsed
    if image_id is not None:
        return resource_edits.build_document(
            source,
            effective,
            int(image_id),
            rendered,
            source_parsed=source_parsed,
            effective_parsed=effective_parsed,
        )
    # The provenance is not known here: it is applied to the finished projection
    # by the one caller that decides whether the document was replaced at all.
    return document_renderer.build(rendered, [], parsed=effective_parsed)


def _source_infotext(image: dict[str, Any], raw: str) -> str:
    """Return the A1111 source structure represented by an image row.

    A1111 text is already the structure the upload needs, including spelling
    the application does not model. Other readers retain their raw container
    document separately, so render their stored reading before applying edits.
    """
    # An A1111 infotext is text; every other family writes a document. Asked
    # through the tolerant reader, because TensorArt writes a trailing NUL byte
    # after its JSON and a strict parse of that fails - which would have sent
    # its whole document out as a prompt.
    if not isinstance(families.first_document(raw), (dict, list)):
        return raw

    parsed = _parsed_reading(image)
    fields = parsed.get("fields") or {}
    parameters = [
        infotext.ParameterEntry(
            key=key,
            value=fields[key],
            raw=compose.render(fields[key]),
            prefix=", ",
            source=None,
        )
        for key in parsed.get("field_order") or []
        if key in fields
    ]
    return compose.render_infotext(
        str(parsed.get("prompt") or ""), parsed.get("negative_prompt"), parameters
    )


def _parsed_reading(image: dict[str, Any]) -> dict[str, Any]:
    """Read the scan's decoded document, with compatibility for bare rows."""
    parsed = image.get("parsed")
    if isinstance(parsed, dict):
        return parsed
    stored = image.get("parsed_json")
    if isinstance(stored, str):
        try:
            parsed = json.loads(stored)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    raw = image.get("raw_infotext") or ""
    for chunks, user_comment in (
        ({infotext.PARAMETERS_KEYWORD: raw}, None),
        ({"prompt": raw}, None),
        ({"invokeai_metadata": raw}, None),
        ({"generation_data": raw}, None),
        ({}, raw),
    ):
        family = png_io.detect_metadata_family(chunks, user_comment)
        if family != png_io.UNKNOWN_METADATA_FAMILY:
            return png_io.read_metadata(
                png_io.ImageFacts(
                    0,
                    0,
                    "application/octet-stream",
                    chunks=chunks,
                    user_comment=user_comment,
                    metadata_family=family,
                )
            )
    return infotext.parse(raw)


def upload_document(image: dict[str, Any]) -> dict[str, Any] | None:
    """The CivitAI ``meta`` rendering of the same model as the upload text."""
    return document_renderer.render_meta(_upload_projection(image))


def _native_upload_text(
    image: dict[str, Any],
    rendered: str,
    resource_rows: tuple[dict[str, Any], ...],
    source_resource_rows: tuple[dict[str, Any], ...],
    source_generator: str | None,
) -> str:
    """Keep the three structures CivitAI reads in their own grammar."""
    raw = image.get("raw_infotext") or ""
    family = _source_family(raw)
    if family not in {swarmui.FAMILY, comfyui.FAMILY, "civitai"}:
        return rendered
    if family in {comfyui.FAMILY, "civitai"} and not _native_png(image):
        return rendered

    original = _parsed_reading(image)
    effective = infotext.parse(rendered)
    if family == swarmui.FAMILY:
        return swarmui.project(raw, original, effective, resource_rows)
    if family == "civitai":
        return civitai_generated.project(
            raw, original, effective, resource_rows, source_resource_rows
        )
    return raw if source_generator is None else rendered


def comfyui_workflow_replaced(
    image: dict[str, Any], *, edit_document: dict[str, Any] | None | object = _STORED_EDIT
) -> bool:
    """Whether the projected upload cannot retain this ComfyUI document.

    ``edit_document`` mirrors `upload_infotext`, whose own callers supply it, and
    it is what lets this question be asked about an edit that is not stored yet.
    """
    raw = image.get("raw_infotext") or ""
    if _source_family(raw) != comfyui.FAMILY:
        return False
    source = _source_infotext(image, raw)
    if edit_document is _STORED_EDIT:
        image_id = image.get("image_id", image.get("id"))
        edit = edits.get(image_id) if image_id is not None else None
    else:
        edit = edit_document
    effective = strip_excluded_fields(compose.apply_edit(source, edit) if edit else source)
    rendered = apply_prompt_exclusions(effective)
    projection = _project_upload_document(image, source, effective, rendered)
    return _comfyui_workflow_replaced(image, projection)


def _comfyui_workflow_replaced(
    image: dict[str, Any], projection: document_renderer.Document
) -> bool:
    """Compare the source document with the full projected upload once."""
    source = _parsed_reading(image)
    projected = infotext.parse(document_renderer.render_infotext(projection))
    return any(
        source.get(key) != projected.get(key)
        for key in ("prompt", "negative_prompt", "fields")
    )


def _native_png(image: dict[str, Any]) -> bool:
    """Whether this graph has the PNG text location CivitAI reads."""
    source_path = _source_path(image)
    if not source_path:
        return True
    facts = png_io.read(Path(source_path))
    return facts is not None and facts.content_type == "image/png"


def _source_path(image: dict[str, Any]) -> str | None:
    """Return the library or post path carried by an image row."""
    return image.get("source_path") or image.get("absolute_path")


def _source_family(raw: str) -> str:
    """Identify a raw container document without assuming where it was stored."""
    replacement = families.replacement_family(raw)
    if replacement is not None:
        return replacement
    for chunks, user_comment in (
        ({"parameters": raw}, None),
        ({"prompt": raw}, None),
        ({}, raw),
    ):
        family = png_io.detect_metadata_family(chunks, user_comment)
        if family != png_io.UNKNOWN_METADATA_FAMILY:
            return family
    return png_io.UNKNOWN_METADATA_FAMILY


def strip_excluded_fields(text: str) -> str:
    """Apply the configured exact-name and trailing-wildcard exclusions."""
    configured = db.get_json_setting(EXCLUDED_FIELDS_SETTING, [])
    if not isinstance(configured, list):
        return text
    patterns = [item.strip() for item in configured if isinstance(item, str) and item.strip()]
    if not patterns:
        return text

    fields = (infotext.parse(text).get("fields") or {}).keys()
    deleted = [
        key
        for key in fields
        if any(
            key.startswith(pattern[:-1]) if pattern.endswith("*") else key == pattern
            for pattern in patterns
        )
    ]
    if not deleted:
        return text
    return compose.apply_edit(text, {"deleted": deleted})


def apply_prompt_exclusions(text: str) -> str:
    """Remove configured regex matches from every parsed prompt field."""
    configured = db.get_json_setting(PROMPT_EXCLUSIONS_SETTING, [])
    if not isinstance(configured, list):
        return text
    expressions = [
        item.strip() for item in configured if isinstance(item, str) and item.strip()
    ]
    if not expressions:
        return text

    parsed = infotext.parse(text)
    draft: dict[str, Any] = {"fields": {}}
    touched: list[str] = []

    for key in ("prompt", "negative_prompt"):
        value = parsed.get(key)
        projected = prompt_tools.exclude_matches(value, expressions)
        if projected != value:
            draft[key] = projected
            touched.append(key)

    for key, value in (parsed.get("fields") or {}).items():
        if not key.casefold().endswith("prompt") or not isinstance(value, str):
            continue
        projected = prompt_tools.exclude_matches(value, expressions)
        if projected != value:
            draft["fields"][key] = projected
            touched.append(key)

    if not touched:
        return text
    return compose.apply_edit(text, {"draft": draft, "touched": touched})


def upload_key(image: dict[str, Any], text: str) -> str:
    """The cache identity of the exact metadata variant being uploaded."""
    original = image["sha256"]
    if text == (image.get("raw_infotext") or ""):
        return original
    return f"{original}:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def path_for(image: dict[str, Any], directory: Path, text: str) -> Path:
    """Materialise the upload metadata in a temporary copy."""
    source = Path(image["source_path"])
    row_id = image.get("id") or image.get("image_id") or "image"
    target = directory / f"{row_id}-{source.name}"
    family = _source_family(image.get("raw_infotext") or "")
    native = family in (comfyui.FAMILY, "civitai") and _native_png(image)
    if family == comfyui.FAMILY:
        native = native and comfyui.graph(text) is not None
    keyword = "prompt" if native else write.PARAMETERS_KEYWORD
    extra_png_text: dict[str, str] = {}
    if family == comfyui.FAMILY and native:
        facts = png_io.read(source)
        if facts is not None and facts.content_type == "image/png":
            workflow = facts.chunks.get("workflow")
            if workflow is not None:
                extra_png_text["workflow"] = workflow
    write.write_copy(source, target, text, keyword=keyword, extra_png_text=extra_png_text)
    return target
