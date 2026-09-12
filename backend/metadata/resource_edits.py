"""Select the resource rows represented by effective upload metadata."""

from __future__ import annotations

from typing import Any

from ..store import images as image_store
from . import infotext, resources, upload_document

RESOURCE_FIELDS = upload_document.RESOURCE_FIELDS


def build_document(
    raw_infotext: str,
    effective_infotext: str,
    image_id: int,
    rendered_infotext: str | None = None,
    source_generator: str | None = None,
    source_parsed: dict[str, Any] | None = None,
    effective_parsed: dict[str, Any] | None = None,
) -> upload_document.Document:
    """Build the shared upload document after applying resource activity rules."""
    original = infotext.parse(raw_infotext) if source_parsed is None else source_parsed
    effective = infotext.parse(effective_infotext) if effective_parsed is None else effective_parsed
    rows = image_store.resources_for(image_id)
    retained = [row for row in rows if _is_retained(row, original, effective)]
    output = effective_infotext if rendered_infotext is None else rendered_infotext
    return upload_document.build(output, rows, retained, source_generator, effective)


def project(raw_infotext: str, effective_infotext: str, image_id: int) -> str:
    """Render the shared upload document as A1111 infotext."""
    return upload_document.render_infotext(
        build_document(raw_infotext, effective_infotext, image_id)
    )


def _is_retained(
    row: dict[str, Any],
    original: dict[str, Any],
    effective: dict[str, Any],
) -> bool:
    """Keep file-side attribution unless a resource actually became inactive."""
    if row.get("deleted_by_user"):
        return False
    if row.get("added_by_user"):
        return True
    kind = row.get("resource_type")
    if kind == resources.CHECKPOINT:
        original_model = (original.get("fields") or {}).get("Model")
        if resources.normalize(row.get("name_in_prompt")) != resources.normalize(
            original_model
        ):
            return True
        model = (effective.get("fields") or {}).get("Model")
        return resources.normalize(row.get("name_in_prompt")) == resources.normalize(model)
    if kind == resources.UPSCALER:
        original_upscaler = (original.get("fields") or {}).get("Hires upscaler")
        if resources.normalize(row.get("name_in_prompt")) != resources.normalize(
            original_upscaler
        ):
            return True
        upscaler = (effective.get("fields") or {}).get("Hires upscaler")
        return resources.normalize(row.get("name_in_prompt")) == resources.normalize(upscaler)
    return True
