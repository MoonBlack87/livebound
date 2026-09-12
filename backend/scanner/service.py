"""The scan job: read folders, hash, parse, thumbnail, store.

One unreadable file must never abort a scan, so every file is ingested inside its
own try/except. Files that disappeared are removed when nothing refers to them;
a post-held row stays as ``is_missing`` so the post remains truthful and usable.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PIL import Image

from .. import db, hashing, jobs, thumbnails
from ..metadata import display, infotext, png_io
from ..metadata import resources as resource_extract
from ..resources import cache as resource_cache
from ..store import images as image_store
from ..store import sources as source_store
from ..store import usage
from .walker import _inside, iter_images

# Raise this whenever png_io, a metadata adapter, infotext parsing,
# metadata/resources.py::extract, or the fields stored by _ingest change.
INGEST_REVISION = 3

_UNREADABLE_IMAGE_MESSAGE = (
    "File could not be read as an image; it may be a truncated download, not an "
    "image, or not be readable with your current permissions."
)


def scan_roots(
    job: jobs.Job,
    root_ids: list[int] | None = None,
    *,
    full: bool = False,
    reconcile_missing: bool = True,
) -> dict[str, Any]:
    """Read the source roots and ingest what changed.

    ``reconcile_missing=False`` is for a pass nobody pressed: it finds new files
    and leaves every conclusion about *disappeared* ones to a scan the
    maintainer started. A file that is merely moved between folders looks gone
    for one pass, and `mark_missing` deletes such a row together with the
    `image_edits` that hang off it - which `PRI-03` and `PRI-04` do not permit
    without a press. The thumbnail prune is skipped for the same reason: it is a
    file deletion, however regenerable.
    """
    roots = source_store.list_roots(enabled_only=True)
    if root_ids:
        roots = [root for root in roots if root["id"] in root_ids]

    totals = {
        "scanned": 0,
        "added": 0,
        "failed": 0,
        "missing": 0,
        "unchanged": 0,
        "reapplied": 0,
    }
    job.total = 0

    trash_folder = _trash_folder()
    totals["missing"] += image_store.remove_vanished_trashed(trash_folder)
    for root in roots:
        base = Path(root["path"])
        if not base.is_dir():
            job.log(root=root["path"], status="skipped", message="folder not found")
            continue
        if trash_folder is not None and _inside(base, trash_folder):
            job.log(root=root["path"], status="skipped", message="folder is inside the trash")
            continue

        job.stage = f"Scanning {base.name}"
        seen: set[int] = set()
        unchanged_rows: list[tuple[int, str, str, str]] = []
        for path in iter_images(
            base,
            excluded=set(root["excluded_folders"]),
            trash_folder=trash_folder,
        ):
            if job.should_stop():
                image_store.mark_seen_many(unchanged_rows)
                return totals
            job.total += 1
            existing = image_store.get_by_path(str(path))
            try:
                image_id, unchanged = _ingest(
                    root["id"], base, path, existing=existing, full=full
                )
                if image_id:
                    seen.add(image_id)
                    totals["scanned"] += 1
                    totals["unchanged"] += int(unchanged)
                    if unchanged:
                        unchanged_rows.append(
                            (
                                image_id,
                                str(path),
                                str(path.relative_to(base)),
                                str(path.parent.relative_to(base))
                                if path.parent != base
                                else "",
                            )
                        )
                elif existing:
                    seen.add(int(existing["id"]))
            except Exception as exc:
                if existing:
                    seen.add(int(existing["id"]))
                totals["failed"] += 1
                job.failed += 1
                job.log(path=str(path), status="error", message=f"{type(exc).__name__}: {exc}")
            job.processed += 1

        image_store.mark_seen_many(unchanged_rows)
        if reconcile_missing and not root.get("is_archive"):
            totals["missing"] += image_store.mark_missing(root["id"], seen)
        source_store.mark_scanned(root["id"])

    if reconcile_missing:
        thumbnails.prune(image_store.content_hashes(), image_store.thumbnail_names())
    totals["reapplied"] = resource_cache.reapply_unresolved()
    job.result = totals
    job.stage = "Done"
    return totals


def _trash_folder() -> Path | None:
    raw = (db.get_setting("trash_folder") or "").strip()
    return Path(raw).resolve() if raw else None


def _ingest(
    root_id: int,
    base: Path,
    path: Path,
    *,
    existing: dict[str, Any] | None = None,
    full: bool = False,
) -> tuple[int | None, bool]:
    stat = path.stat()
    sha256 = hashing.file_sha256(path)
    unchanged = bool(
        existing
        and not existing.get("is_missing")
        and existing.get("pixel_sha256")
        and existing.get("phash")
        and existing.get("file_mtime") == stat.st_mtime
        and existing.get("file_size") == stat.st_size
        and existing.get("sha256") == sha256
    )
    if (
        not full
        and unchanged
        and existing.get("ingest_revision") == INGEST_REVISION
    ):
        return int(existing["id"]), True

    try:
        image = Image.open(path)
    except Exception as exc:
        # Raising is what makes it visible: the caller counts this file in
        # `failed` and writes it to the job log with its path. It still does not
        # abort the batch - that handler catches per file, by design.
        raise ValueError(_UNREADABLE_IMAGE_MESSAGE) from exc
    with image:
        facts = png_io.read(path, image=image)
        if facts is None:
            raise ValueError(_UNREADABLE_IMAGE_MESSAGE)
        parsed = png_io.read_metadata(facts)
        pixel_sha256 = (
            existing["pixel_sha256"]
            if unchanged
            else hashing.pixel_sha256(path, image=image)
        )
        phash = (
            existing["phash"]
            if unchanged and existing.get("phash")
            else hashing.dhash64(path, image=image)
        )

    record = {
        "absolute_path": str(path),
        "relative_path": str(path.relative_to(base)),
        "folder": str(path.parent.relative_to(base)) if path.parent != base else "",
        "file_mtime": stat.st_mtime,
        "file_size": stat.st_size,
        "content_type": facts.content_type,
        "width": facts.width,
        "height": facts.height,
        "sha256": sha256,
        "pixel_sha256": pixel_sha256,
        "phash": phash,
        "raw_infotext": facts.infotext,
        "parsed": parsed,
        "ingest_revision": INGEST_REVISION,
        "thumbnail_path": thumbnails.ensure(path, sha256),
    }
    image_id = image_store.upsert(root_id, record)
    image_store.record_metadata_fields(image_id, parsed.get("fields", {}).keys())
    if record["pixel_sha256"] and record["sha256"]:
        # The dedup memory is the usage store's to write. The v12 migration left
        # this column empty on purpose; the scan has just decoded the image, so
        # this is the cheap moment to fill it for the same bytes.
        usage.fill_pixel_hash(record["sha256"], record["pixel_sha256"])

    found = resource_extract.extract(parsed)
    resource_cache.enrich(found)
    image_store.replace_resources(image_id, found)
    return image_id, False


def image_view(image: dict[str, Any]) -> dict[str, Any]:
    """The readable, CivitAI-shaped metadata view for one image."""
    parsed = image.get("parsed") or infotext.parse(image.get("raw_infotext"))
    return display.render(parsed, image_store.resources_for(image["id"]))
