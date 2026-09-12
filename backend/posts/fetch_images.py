"""Fetch a post's images from CivitAI when they do not exist locally.

Generate straight on CivitAI and post immediately, and the files were never on
disk. Without them the archive stays incomplete - the post is there, the images
are missing. This run gets them.

They land in the maintainer's adopted folder regardless of the post's state.
Published files reach the archive only through the ordinary explicit archive
move, so the file location and post state change together.

Videos are out of scope. They are handled outside this app, and the base64 path
could not move them anyway - a 750 MB file would make a gigabyte of JSON. A mixed
post loses nothing by it: the video row stays, it is simply not fetched.

Downloaded files are taken into the library, so from then on they are treated like
any other image - duplicate check included.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import config, db, hashing, jobs, paths, thumbnails
from ..civitai import media
from ..metadata import infotext, png_io
from ..metadata import resources as resource_extract
from ..resources import cache as resource_cache
from ..store import images as image_store
from ..store import posts as post_store
from ..store import sources as source_store
from ..store import usage

#: Suffixes we derive from a MIME type. Everything else becomes .png.
_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
}


def run(job: jobs.Job, post_id: int) -> dict[str, Any]:
    """Fetch the missing images and take them into the library."""
    post = post_store.get(post_id)
    if post is None:
        job.error = "Post not found."
        return {}

    target, reason = _destination(post)
    if target is None:
        job.error = reason or "No destination could be determined."
        return {}

    all_rows = post_store.images(post_id)
    rows = [row for row in all_rows if _needs_fetch(row)]
    videos = sum(1 for row in all_rows if is_video(row) and not row.get("image_id"))
    job.total = len(rows)
    totals = {
        "fetched": 0,
        "skipped": 0,
        "videos": videos,
        "failed": 0,
        "target": str(target),
    }
    if videos:
        job.log(
            status="info",
            message=f"{videos} video(s) skipped - this app does not handle them.",
        )

    folder = target / f"post_id_{post['remote_post_id']}"
    folder.mkdir(parents=True, exist_ok=True)

    for index, row in enumerate(rows, start=1):
        if job.should_stop():
            break
        job.processed = index
        job.stage = f"Image {index}/{len(rows)}"
        try:
            path = _fetch_one(folder, row, post)
        except Exception as exc:
            totals["failed"] += 1
            job.failed += 1
            job.log(status="error", message=f"{type(exc).__name__}: {exc}")
            continue
        if path is None:
            totals["skipped"] += 1
            continue
        totals["fetched"] += 1
        job.succeeded += 1
        job.log(status="ok", path=str(path))

    job.result = totals
    job.stage = "Done"
    return totals


def is_video(row: dict[str, Any]) -> bool:
    """Video or audio - everything this app does not touch."""
    if str(row.get("media_type") or "image") != "image":
        return True
    return str(row.get("content_type") or "").startswith(("video/", "audio/"))


def _needs_fetch(row: dict[str, Any]) -> bool:
    if row.get("image_id"):
        return False
    if not row.get("remote_url"):
        return False
    # Videos are handled outside this app.
    if is_video(row):
        return False
    # A row with a path whose file still exists is not orphaned.
    path = row.get("source_path")
    return not (path and Path(path).exists())


def _is_published(post: dict[str, Any]) -> bool:
    from . import schedule

    when = schedule.parse_iso(post.get("remote_published_at"))
    return when is not None and when <= schedule.utcnow()


def _destination(post: dict[str, Any]) -> tuple[Path | None, str | None]:
    """The configured adopted folder, independent of publication state."""
    if not post.get("remote_post_id"):
        return None, "The post is not on CivitAI."

    folder = config.adopted_images_dir()
    if folder is None or not folder.is_dir():
        return None, (
            "Choose an available adopted folder in Settings before fetching images."
        )
    return folder, None


def _fetch_one(folder: Path, row: dict[str, Any], post: dict[str, Any]) -> Path | None:
    url = str(row["remote_url"]).rsplit("/", 1)[0] + "/original=true"
    target = folder / _filename(row)

    final, _ = paths.free_name(target)
    media.download(url, final)

    record = _index(final, post, row)
    with db.transaction() as conn:
        conn.execute(
            "UPDATE post_images SET image_id=?, source_path=?, sha256=?, phash=?,"
            " file_size=?, content_type=? WHERE id=?",
            (
                record["id"],
                str(final),
                record["sha256"],
                record["phash"],
                record["file_size"],
                record["content_type"],
                row["id"],
            ),
        )
    return final


def _filename(row: dict[str, Any]) -> str:
    """Keep the remote name; the archive move appends the image id later.

    When CivitAI knows no name - the normal case for images generated there -
    the upload key takes its place.
    """
    suffix = _EXTENSIONS.get(row.get("content_type") or "", ".png")
    stem = Path(str(row.get("source_path") or "")).stem or str(row.get("remote_uuid") or "image")
    return f"{stem}{suffix}"


def _index(path: Path, post: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    """Take the downloaded file in as if it had been scanned.

    From here on it is an image like any other: with hashes, metadata and a
    thumbnail, and therefore part of the duplicate check.
    """
    root = _root_for(path) or _fallback_root()
    stat = path.stat()
    facts = png_io.read(path)
    parsed = infotext.parse(facts.infotext if facts else None)

    relative = path.relative_to(Path(root["path"]))
    sha256 = hashing.file_sha256(path)
    record = {
        "absolute_path": str(path),
        "relative_path": str(relative),
        "folder": str(relative.parent) if str(relative.parent) != "." else "",
        "file_mtime": stat.st_mtime,
        "file_size": stat.st_size,
        "content_type": facts.content_type if facts else row.get("content_type"),
        "width": facts.width if facts else row.get("width"),
        "height": facts.height if facts else row.get("height"),
        "sha256": sha256,
        "pixel_sha256": hashing.pixel_sha256(path),
        "phash": hashing.dhash64(path),
        "raw_infotext": facts.infotext if facts else None,
        "parsed": parsed,
        "thumbnail_path": thumbnails.ensure(path, sha256),
    }
    image_id = image_store.upsert(root["id"], record)

    found = resource_extract.extract(parsed)
    resource_cache.enrich(found)
    image_store.replace_resources(image_id, found)

    # It is on CivitAI - so it belongs in the history, or the same file would
    # later count as unused.
    identity = usage.generation_identity(parsed)
    usage.record(
        sha256=record["sha256"],
        pixel_sha256=record["pixel_sha256"],
        phash=record["phash"],
        post_id=post["id"],
        remote_post_id=post.get("remote_post_id"),
        remote_image_id=row.get("remote_image_id"),
        source_path=str(path),
        post_title=post.get("title"),
        status=usage.PUBLISHED if _is_published(post) else usage.PUSHED,
        origin="fetch",
        **identity,
    )
    return {**record, "id": image_id}


def _root_for(path: Path) -> dict[str, Any] | None:
    resolved = path.resolve()
    for root in source_store.list_roots():
        base = Path(root["path"]).resolve()
        if base == resolved or base in resolved.parents:
            return root
    return None


def _fallback_root() -> dict[str, Any]:
    """The adopted folder is normally not a source - here it becomes one.

    Otherwise the files would have no place in the library, and the duplicate
    check would know nothing about them.
    """
    folder = config.adopted_images_dir()
    if folder is None:
        raise RuntimeError("Choose an adopted folder in Settings and retry the fetch.")
    return source_store.add_root(str(folder), label="Adopted images")
