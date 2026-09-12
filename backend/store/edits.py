"""Metadata the user changed, kept apart from what the file says.

The file is never rewritten. An edit lives here until the moment of an upload,
when a temporary copy carrying it is made and thrown away again afterwards. Both
the original state and the change stay available, and the picture exists once on
disk rather than twice.

Only the differences are stored, never a whole document. The file remains the
source: if a rescan finds a value the user never touched, that new value is what
shows - a stored copy of the old one would quietly overrule it.
"""

from __future__ import annotations

import json
from typing import Any

from .. import db

#: An empty *draft*: the two prompts plus the parameter fields, all optional.
#: This is what lives under an edit's ``draft`` key - not an edit itself.
EMPTY: dict[str, Any] = {"prompt": None, "negative_prompt": None, "fields": {}}


def empty_document() -> dict[str, Any]:
    """An edit that changes nothing, in the shape :func:`get` returns.

    Named, because the draft and the document around it are both dictionaries
    and confusing them is silent: the API handed a draft out as an edit, and the
    drawer read ``edit.draft.prompt`` off it and threw on the first change to any
    image that had never been edited.
    """
    return {"draft": dict(EMPTY), "touched": [], "deleted": []}


def get(image_id: int) -> dict[str, Any] | None:
    row = (
        db.get_connection()
        .execute("SELECT * FROM image_edits WHERE image_id=?", (image_id,))
        .fetchone()
    )
    return _row(row) if row else None


def get_many(image_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not image_ids:
        return {}
    placeholders = ",".join("?" * len(image_ids))
    rows = db.get_connection().execute(
        f"SELECT * FROM image_edits WHERE image_id IN ({placeholders})", image_ids
    )
    return {row["image_id"]: _row(row) for row in rows}


def save(
    image_id: int,
    *,
    draft: dict[str, Any],
    touched: list[str],
    deleted: list[str],
) -> dict[str, Any]:
    """Write the edit, or drop the row entirely when nothing is left of it.

    An empty edit is deleted rather than stored: "this image has no changes" and
    "this image has an edit that happens to change nothing" are the same thing to
    every reader, and one of them is easier to reason about.
    """
    if not touched and not deleted:
        clear(image_id)
        return empty_document()

    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO image_edits(image_id, draft_json, touched_fields_json,"
            " deleted_fields_json, updated_at) VALUES(?,?,?,?,?)"
            " ON CONFLICT(image_id) DO UPDATE SET"
            " draft_json=excluded.draft_json,"
            " touched_fields_json=excluded.touched_fields_json,"
            " deleted_fields_json=excluded.deleted_fields_json,"
            " updated_at=excluded.updated_at",
            (
                image_id,
                json.dumps(draft, ensure_ascii=False),
                json.dumps(sorted(set(touched)), ensure_ascii=False),
                json.dumps(sorted(set(deleted)), ensure_ascii=False),
                db.now_iso(),
            ),
        )
    return get(image_id) or empty_document()


def clear(image_id: int) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM image_edits WHERE image_id=?", (image_id,))


def edited_ids(image_ids: list[int]) -> set[int]:
    """Which of these images carry a metadata edit or a resource override.

    Two sources, one answer. A prompt change lives in `image_edits`; a decision
    about a model lives as a flag on the `image_resources` row, and both change
    what the upload carries. One query each, for a whole page - this is called
    once per library page and must not become a query per image.
    """
    from . import images as image_store

    return set(get_many(image_ids)) | image_store.resource_override_ids(image_ids)


def count() -> int:
    return int(
        db.get_connection().execute("SELECT COUNT(*) AS n FROM image_edits").fetchone()["n"]
    )


def _row(row: Any) -> dict[str, Any]:
    return {
        "image_id": row["image_id"],
        "draft": _json(row["draft_json"], dict(EMPTY)),
        "touched": _json(row["touched_fields_json"], []),
        "deleted": _json(row["deleted_fields_json"], []),
        "updated_at": row["updated_at"],
    }


def _json(raw: str | None, fallback: Any) -> Any:
    try:
        return json.loads(raw) if raw else fallback
    except ValueError:
        return fallback
