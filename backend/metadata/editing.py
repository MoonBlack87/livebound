"""Build, compare and bulk-plan sparse image metadata edits."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from ..posts import materialise
from ..store import edits
from . import infotext, linter, prompt_tools, resource_edits, upload_document

PROMPT_KEYS = ("prompt", "negative_prompt")
DERIVED_FIELDS = {*resource_edits.RESOURCE_FIELDS, upload_document.LIVEBOUND_KEY}


def view(image: dict[str, Any]) -> dict[str, Any]:
    original = image.get("parsed") or infotext.parse(image.get("raw_infotext") or "")
    edit = edits.get(image["id"]) or edits.empty_document()
    effective_text = materialise.upload_infotext(image)
    effective = infotext.parse(effective_text)
    return {
        "image_id": image["id"],
        "edit": edit,
        "original": original,
        "effective": effective,
        "diff": diff(original, effective, edit),
        "effective_infotext": effective_text,
        "warnings": linter.lint(effective),
        "comfyui_workflow_replaced": materialise.comfyui_workflow_replaced(image),
    }


def diff(
    original: dict[str, Any], effective: dict[str, Any], edit: dict[str, Any]
) -> dict[str, Any]:
    touched = set(edit.get("touched") or [])
    deleted = set(edit.get("deleted") or [])
    prompts = {
        key: {
            "original": original.get(key),
            "value": effective.get(key),
            "changed": key in touched or key in deleted,
            "deleted": key in deleted,
        }
        for key in PROMPT_KEYS
    }
    old_fields = original.get("fields") or {}
    new_fields = effective.get("fields") or {}
    order = list(dict.fromkeys([
        *(original.get("field_order") or []),
        *(effective.get("field_order") or []),
        *sorted(set(old_fields) | set(new_fields)),
    ]))
    fields = [
        {
            "key": key,
            "original": old_fields.get(key),
            "value": new_fields.get(key),
            "changed": key in touched or key in deleted,
            "deleted": key in deleted,
            "derived": key in DERIVED_FIELDS,
        }
        for key in order
    ]
    return {"prompts": prompts, "fields": fields, "changed": bool(touched or deleted)}


def normalise(
    image: dict[str, Any], draft: dict[str, Any], touched: list[str], deleted: list[str]
) -> dict[str, Any]:
    """Keep only real differences and return a store-ready edit."""
    original = image.get("parsed") or infotext.parse(image.get("raw_infotext") or "")
    source_fields = original.get("fields") or {}
    clean_draft: dict[str, Any] = {"prompt": None, "negative_prompt": None, "fields": {}}
    clean_touched: list[str] = []
    clean_deleted: list[str] = []
    derived = DERIVED_FIELDS
    deleted_set = {str(key) for key in deleted} - derived
    touched_set = {str(key) for key in touched} - deleted_set - derived
    draft_fields = draft.get("fields") if isinstance(draft.get("fields"), dict) else {}

    for key in PROMPT_KEYS:
        if key in deleted_set:
            if original.get(key) is not None:
                clean_deleted.append(key)
        elif key in touched_set:
            value = draft.get(key)
            if value != original.get(key):
                clean_draft[key] = value
                clean_touched.append(key)

    for key in sorted((touched_set | deleted_set) - set(PROMPT_KEYS)):
        if key in deleted_set:
            if key in source_fields:
                clean_deleted.append(key)
        elif key in draft_fields and draft_fields[key] != source_fields.get(key):
            clean_draft["fields"][key] = draft_fields[key]
            clean_touched.append(key)

    return {"draft": clean_draft, "touched": clean_touched, "deleted": clean_deleted}


def save(image: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    normal = normalise(
        image,
        payload.get("draft") if isinstance(payload.get("draft"), dict) else {},
        payload.get("touched") or [],
        payload.get("deleted") or [],
    )
    edits.save(image["id"], **normal)
    return view(image)


def update_value(edit: dict[str, Any], key: str, value: Any) -> dict[str, Any]:
    result = deepcopy(edit)
    result.setdefault("draft", {"prompt": None, "negative_prompt": None, "fields": {}})
    result.setdefault("touched", [])
    result.setdefault("deleted", [])
    if key in PROMPT_KEYS:
        result["draft"][key] = value
    else:
        result["draft"].setdefault("fields", {})[key] = value
    result["touched"] = [*result["touched"], key]
    result["deleted"] = [item for item in result["deleted"] if item != key]
    return result


def delete_value(edit: dict[str, Any], key: str) -> dict[str, Any]:
    result = deepcopy(edit)
    result.setdefault("draft", {"prompt": None, "negative_prompt": None, "fields": {}})
    result["touched"] = [item for item in result.get("touched", []) if item != key]
    result["deleted"] = [*result.get("deleted", []), key]
    return result


def restore_value(edit: dict[str, Any], key: str) -> dict[str, Any]:
    result = deepcopy(edit)
    result["touched"] = [item for item in result.get("touched", []) if item != key]
    result["deleted"] = [item for item in result.get("deleted", []) if item != key]
    if key in PROMPT_KEYS:
        result.get("draft", {}).pop(key, None)
    else:
        result.get("draft", {}).get("fields", {}).pop(key, None)
    return result


def bulk_plan(images: list[dict[str, Any]], operations: dict[str, Any]) -> list[dict[str, Any]]:
    plan = []
    prompt_values = operations.get("prompt_values") or {}
    for image in images:
        before = view(image)
        candidate = deepcopy(before["edit"])
        for key, value in (operations.get("fields") or {}).items():
            candidate = update_value(candidate, key, value)
        for key in operations.get("delete_fields") or []:
            candidate = delete_value(candidate, key)
        for key in operations.get("restore_fields") or []:
            candidate = restore_value(candidate, key)

        effective = before["effective"]
        rule = operations.get("find_replace") or {}
        if rule.get("find"):
            target = rule.get("target", "prompt")
            if target in ("prompt", "both"):
                candidate = update_value(
                    candidate,
                    "prompt",
                    prompt_tools.find_replace(
                        effective.get("prompt"),
                        rule["find"],
                        rule.get("replace", ""),
                        regex=bool(rule.get("regex")),
                        case_sensitive=bool(rule.get("case_sensitive")),
                    ),
                )
            if target in ("negative", "both"):
                candidate = update_value(
                    candidate,
                    "negative_prompt",
                    prompt_tools.find_replace(
                        effective.get("negative_prompt"),
                        rule["find"],
                        rule.get("replace", ""),
                        regex=bool(rule.get("regex")),
                        case_sensitive=bool(rule.get("case_sensitive")),
                    ),
                )
        if operations.get("prompt_prepend") or operations.get("prompt_append"):
            candidate = update_value(
                candidate,
                "prompt",
                prompt_tools.join_prompt(
                    effective.get("prompt") or "",
                    operations.get("prompt_prepend"),
                    operations.get("prompt_append"),
                ),
            )
        if operations.get("negative_append"):
            candidate = update_value(
                candidate,
                "negative_prompt",
                prompt_tools.join_prompt(
                    effective.get("negative_prompt") or "", None, operations["negative_append"]
                ),
            )
        suggested = prompt_values.get(str(image["id"]), prompt_values.get(image["id"]))
        if suggested is not None:
            candidate = update_value(candidate, "prompt", suggested)

        normal = normalise(
            image,
            candidate.get("draft") or {},
            candidate.get("touched") or [],
            candidate.get("deleted") or [],
        )
        after_text = materialise.upload_infotext(image, edit_document=normal)
        after = infotext.parse(after_text)
        before_state = before["effective"]
        changes = _changes(before_state, after)
        plan.append(
            {
                "image_id": image["id"],
                "filename": image.get("relative_path", "").rsplit("/", 1)[-1],
                "changes": changes,
                "edit": normal,
                # What this image actually carried, so the caller can tell a
                # field name that matches nothing from one that changed nothing.
                "fields_present": sorted(before_state.get("fields") or {}),
            }
        )
    return plan


def apply_plan(plan: list[dict[str, Any]]) -> int:
    changed = 0
    for item in plan:
        edit = item["edit"]
        edits.save(item["image_id"], **edit)
        if item["changes"]:
            changed += 1
    return changed


def _changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    result = []
    for key in PROMPT_KEYS:
        if before.get(key) != after.get(key):
            result.append({"field": key, "before": before.get(key), "after": after.get(key)})
    old_fields, new_fields = before.get("fields") or {}, after.get("fields") or {}
    for key in sorted(set(old_fields) | set(new_fields)):
        if old_fields.get(key) != new_fields.get(key):
            result.append(
                {"field": key, "before": old_fields.get(key), "after": new_fields.get(key)}
            )
    return result
