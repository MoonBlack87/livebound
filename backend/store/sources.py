"""Source folders: the roots that get scanned for postable images."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .. import db


def archive_root() -> dict[str, Any] | None:
    """The folder archived images are moved into, if one is configured."""
    row = db.get_connection().execute(
        "SELECT * FROM source_roots WHERE is_archive=1 ORDER BY id LIMIT 1"
    ).fetchone()
    return _row(row) if row else None


def list_roots(*, enabled_only: bool = False) -> list[dict[str, Any]]:
    sql = "SELECT * FROM source_roots"
    if enabled_only:
        sql += " WHERE enabled=1"
    sql += " ORDER BY id"
    return [_row(row) for row in db.get_connection().execute(sql)]


def get_root(root_id: int) -> dict[str, Any] | None:
    row = db.get_connection().execute(
        "SELECT * FROM source_roots WHERE id=?", (root_id,)
    ).fetchone()
    return _row(row) if row else None


def add_root(
    path: str,
    *,
    label: str = "",
    excluded: list[str] | None = None,
    is_archive: bool = False,
) -> dict[str, Any]:
    resolved = str(Path(path).expanduser().resolve())
    exclusions = list(dict.fromkeys(excluded or []))
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO source_roots(path, label, enabled, excluded_folders,"
            " is_archive, created_at) VALUES(?,?,1,?,?,?) ON CONFLICT(path) DO UPDATE SET"
            " label=excluded.label,"
            " excluded_folders=excluded.excluded_folders, is_archive=excluded.is_archive,"
            " enabled=1",
            (
                resolved,
                label,
                json.dumps(exclusions),
                1 if is_archive else 0,
                db.now_iso(),
            ),
        )
    row = db.get_connection().execute(
        "SELECT * FROM source_roots WHERE path=?", (resolved,)
    ).fetchone()
    return _row(row)


def delete_root(root_id: int) -> None:
    with db.transaction() as conn:
        conn.execute("DELETE FROM source_roots WHERE id=?", (root_id,))


def deletion_impact(root_id: int) -> dict[str, int]:
    row = db.get_connection().execute(
        """
        WITH target_images AS (
            SELECT id FROM images WHERE source_root_id=?
        )
        SELECT
            (SELECT COUNT(*) FROM target_images) AS images,
            (SELECT COUNT(*) FROM image_edits
             WHERE image_id IN (SELECT id FROM target_images)) AS edits,
            (SELECT COUNT(*) FROM image_resources
             WHERE image_id IN (SELECT id FROM target_images)
               AND (locked_by_user=1 OR added_by_user=1 OR deleted_by_user=1))
                AS resource_overrides
        """,
        (root_id,),
    ).fetchone()
    return {
        "images": int(row["images"]),
        "edits": int(row["edits"]),
        "resource_overrides": int(row["resource_overrides"]),
    }


def mark_scanned(root_id: int) -> None:
    with db.transaction() as conn:
        conn.execute(
            "UPDATE source_roots SET last_scanned_at=? WHERE id=?", (db.now_iso(), root_id)
        )


def set_watch_enabled(root_id: int, enabled: bool) -> None:
    """Switch the periodic re-scan for one folder. Off is the default."""
    with db.transaction() as conn:
        conn.execute(
            "UPDATE source_roots SET watch_enabled=? WHERE id=?",
            (1 if enabled else 0, root_id),
        )


def _row(row: Any) -> dict[str, Any]:
    value = dict(row)
    try:
        value["excluded_folders"] = json.loads(value.get("excluded_folders") or "[]")
    except ValueError:
        value["excluded_folders"] = []
    value["enabled"] = bool(value.get("enabled"))
    # Folders are always read recursively (IMG-02). The column is still in the
    # table, unread; it has no place in what the application hands out.
    value.pop("recursive", None)
    value["is_archive"] = bool(value.get("is_archive"))
    value["watch_enabled"] = bool(value.get("watch_enabled"))
    value["exists"] = Path(value["path"]).is_dir()
    return value
