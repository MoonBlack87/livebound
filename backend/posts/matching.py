"""Match the images of an adopted post to local files.

A post adopted from CivitAI knows its images only as delivery URLs. Often the same
pictures have long been sitting in the source folders - they were the files the
post was made from. Finding them is worth it: only then does the UI show the local
file instead of a network fetch, and only then does the app know which file belongs
to which image.

Two stages, in this order, because they cost very differently:

1. **From the history.** If the image was ever recorded - by the backfill or by a
   push of our own - its CivitAI id sits there next to the hash. That is one query
   and needs no network.
2. **Download and hash.** Otherwise the file is fetched, hashed and looked up by
   identical bytes or identical decoded RGB pixels. It is deleted right
   afterwards; only the fingerprints are needed.
"""

from __future__ import annotations

import contextlib
import re
import tempfile
from pathlib import Path
from typing import Any

from .. import db, hashing, jobs
from ..civitai import media, trpc
from ..civitai.errors import AuthError, CivitaiError, RateLimited
from ..store import images as image_store
from ..store import posts as post_store
from ..store import usage

_MAX_SUGGESTION_DHASH_DISTANCE = 7
_REASON_SCORE = {
    "identical_bytes": 10_000,
    "identical_pixels": 9_000,
    "same_seed_prompt": 800,
    "close_dhash": 500,
    "same_prompt": 260,
    "same_seed": 180,
    "remote_filename": 120,
    "same_dimensions": 20,
}


class ManualMatchError(RuntimeError):
    """A stable, user-actionable refusal of a manual image match."""

    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status


def resolve(post_id: int, *, download: bool = True) -> dict[str, Any]:
    """For every image without a local file, look for the match in the library."""
    from .fetch_images import is_video

    rows = [
        row
        for row in post_store.images(post_id)
        if row.get("image_id") is None and row.get("remote_image_id") and not is_video(row)
    ]
    result = {
        "checked": len(rows), "matched": 0, "from_history": 0, "downloaded": 0, "unreadable": 0
    }
    if not rows:
        return result

    with tempfile.TemporaryDirectory(prefix="livebound-match-") as scratch:
        folder = Path(scratch)
        for row in rows:
            local = _from_history(row["remote_image_id"])
            source = "history"
            if local is None and download and row.get("remote_url"):
                local = _by_download(folder, row, result)
                source = "download"
            if local is None:
                continue

            _link(row["id"], local, post_id)
            result["matched"] += 1
            result["from_history" if source == "history" else "downloaded"] += 1

    return result


def unmatched_post_ids() -> list[int]:
    """Posts that still have an image without a local file."""
    # Archived posts are finished. A pushing post belongs to its active push run,
    # so the batch matcher must leave it untouched until that run has finished.
    return [
        int(row["post_id"])
        for row in db.get_connection().execute(
            "SELECT DISTINCT pi.post_id FROM post_images pi"
            " JOIN posts p ON p.id = pi.post_id"
            " WHERE pi.image_id IS NULL AND pi.remote_image_id IS NOT NULL"
            "   AND p.state NOT IN ('archived', 'pushing')"
            " ORDER BY pi.post_id"
        )
    ]


def resolve_all(job: jobs.Job, *, download: bool = True) -> dict[str, Any]:
    """Run the per-post search across every post that has something to find.

    Adding a source folder later leaves every post adopted before it without
    local files, and going into each one to press the same button is the work
    this takes over. Nothing is downloaded permanently: a file that cannot be
    identified is simply left alone, exactly as in the single-post action.
    """
    post_ids = unmatched_post_ids()
    job.total = len(post_ids)
    totals = {"posts": len(post_ids), "matched": 0, "checked": 0, "unreadable": 0}

    for index, post_id in enumerate(post_ids, start=1):
        if job.should_stop():
            break
        job.processed = index
        job.stage = f"Post {index}/{len(post_ids)}"
        try:
            result = resolve(post_id, download=download)
        except (AuthError, RateLimited):
            # Every remaining post would fail the same way; saying "nothing
            # found" to a refused key would be a lie the user acts on.
            raise
        # One post's failure must not end a run the user asked for.
        except Exception as exc:
            job.failed += 1
            job.log(post_id=post_id, status="error", message=f"{type(exc).__name__}: {exc}")
            continue
        for key in ("matched", "checked", "unreadable"):
            totals[key] += result[key]
        if result["matched"]:
            job.succeeded += 1
            job.log(post_id=post_id, status="ok", matched=result["matched"],
                    checked=result["checked"])
        else:
            job.skipped += 1

    job.result = totals
    job.stage = "Done"
    return totals


def suggestions(post_id: int, post_image_id: int, *, limit: int = 24) -> dict[str, Any]:
    """Rank plausible local files without turning evidence into identity.

    The one remote image is downloaded and decoded once. Local files are never
    opened here: their stored hashes and parsed metadata are enough, and close
    dHash candidates come from the eight indexed bands. The result is therefore
    suitable for an on-demand picker even with thousands of indexed files.
    """
    post, remote_row = _match_context(post_id, post_image_id)
    owner_image = _owner_image(post, remote_row)
    owner_meta = owner_image.get("meta") if isinstance(owner_image, dict) else None
    owner_meta = owner_meta if isinstance(owner_meta, dict) else {}
    remote_identity = {
        "seed": None
        if owner_meta.get("seed") in (None, "")
        else str(owner_meta.get("seed")),
        "prompt_hash": usage.prompt_fingerprint(owner_meta.get("prompt")),
    }

    remote_fingerprints: dict[str, str | None] = {
        "sha256": None,
        "pixel_sha256": None,
        "phash": None,
    }
    if remote_row.get("remote_url"):
        with tempfile.TemporaryDirectory(prefix="livebound-suggest-") as scratch:
            target = Path(scratch) / f"{remote_row.get('remote_image_id') or post_image_id}.bin"
            # Suggestions are optional evidence. A failed remote read must not
            # take away the full-library manual picker below them.
            with contextlib.suppress(CivitaiError, OSError, ValueError):
                remote_fingerprints = _download_fingerprints(target, remote_row)

    remote_phash = remote_fingerprints.get("phash")
    phash_candidate_ids = _phash_candidate_ids(remote_phash)
    width = _positive_int((owner_image or {}).get("width")) or _positive_int(
        remote_row.get("width")
    )
    height = _positive_int((owner_image or {}).get("height")) or _positive_int(
        remote_row.get("height")
    )
    remote_name = (owner_image or {}).get("name")

    ranked: list[tuple[int, int, dict[str, Any], list[dict[str, Any]]]] = []
    for local in image_store.search_candidates():
        reasons: list[dict[str, Any]] = []
        score = 0
        core_evidence = False

        remote_sha = remote_fingerprints.get("sha256")
        remote_pixels = remote_fingerprints.get("pixel_sha256")
        if remote_sha and local.get("sha256") == remote_sha:
            reasons.append({"code": "identical_bytes"})
            score += _REASON_SCORE["identical_bytes"]
            core_evidence = True
        elif remote_pixels and local.get("pixel_sha256") == remote_pixels:
            reasons.append({"code": "identical_pixels"})
            score += _REASON_SCORE["identical_pixels"]
            core_evidence = True

        local_identity = usage.generation_identity(local.get("parsed"))
        same_seed = bool(
            remote_identity["seed"]
            and local_identity["seed"] == remote_identity["seed"]
        )
        same_prompt = bool(
            remote_identity["prompt_hash"]
            and local_identity["prompt_hash"] == remote_identity["prompt_hash"]
        )
        if same_seed and same_prompt:
            reasons.append({"code": "same_seed_prompt"})
            score += _REASON_SCORE["same_seed_prompt"]
            core_evidence = True
        elif same_prompt:
            reasons.append({"code": "same_prompt"})
            score += _REASON_SCORE["same_prompt"]
            core_evidence = True
        elif same_seed:
            reasons.append({"code": "same_seed"})
            score += _REASON_SCORE["same_seed"]
            core_evidence = True

        distance = 64
        if local["id"] in phash_candidate_ids:
            distance = hashing.hamming(remote_phash, local.get("phash"))
            if distance <= min(usage.threshold(), _MAX_SUGGESTION_DHASH_DISTANCE):
                reasons.append({"code": "close_dhash", "distance": distance})
                score += _REASON_SCORE["close_dhash"] - distance * 25
                core_evidence = True

        if _filename_has_remote_token(local["relative_path"], remote_row) or _same_name(
            local["relative_path"], remote_name
        ):
            reasons.append({"code": "remote_filename"})
            score += _REASON_SCORE["remote_filename"]
            core_evidence = True

        if width and height and local.get("width") == width and local.get("height") == height:
            reasons.append({"code": "same_dimensions"})
            score += _REASON_SCORE["same_dimensions"]

        if core_evidence:
            ranked.append((score, distance, local, reasons))

    ranked.sort(
        key=lambda item: (
            -item[0],
            item[1],
            str(item[2].get("relative_path") or "").casefold(),
            item[2]["id"],
        )
    )
    selected = ranked[:limit]
    marks = usage.check_many(
        [
            {
                "sha256": local.get("sha256"),
                "pixel_sha256": local.get("pixel_sha256"),
                "phash": local.get("phash"),
            }
            for _, _, local, _ in selected
        ]
    )
    items = [
        {
            "id": local["id"],
            "relative_path": local["relative_path"],
            "width": local.get("width"),
            "height": local.get("height"),
            "file_size": local.get("file_size"),
            "thumbnail_path": local.get("thumbnail_path"),
            "used": bool(marks.get(local.get("sha256") or "", {}).get("has_certain")),
            "reasons": reasons,
        }
        for _, _, local, reasons in selected
    ]
    return {
        "items": items,
        "remote_facts": {
            "seed": remote_identity["seed"] is not None,
            "prompt": remote_identity["prompt_hash"] is not None,
            "dhash": remote_phash is not None,
        },
    }


def link_manual(post_id: int, post_image_id: int, image_id: int) -> None:
    """Attach the local file explicitly chosen by the user."""
    _, _remote = _match_context(post_id, post_image_id)
    local = image_store.get(image_id)
    if local is None:
        raise ManualMatchError("image_not_found", "Image not found.", 404)
    if local.get("is_missing") or local.get("is_trashed"):
        raise ManualMatchError(
            "local_image_unavailable",
            "The local image is unavailable. Rescan the source folder and choose it again.",
        )
    duplicate = next(
        (
            row
            for row in post_store.images(post_id)
            if row["id"] != post_image_id and row.get("image_id") == image_id
        ),
        None,
    )
    if duplicate is not None:
        raise ManualMatchError(
            "image_already_in_post",
            "That local image is already linked to another image in this post. "
            "Choose a different file.",
        )
    _link(post_image_id, local, post_id)


def _match_context(
    post_id: int, post_image_id: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    from .fetch_images import is_video

    post = post_store.get(post_id)
    if post is None:
        raise ManualMatchError("post_not_found", "Post not found.", 404)
    remote = next(
        (row for row in post_store.images(post_id) if row["id"] == post_image_id),
        None,
    )
    if remote is None:
        raise ManualMatchError(
            "post_image_not_found", "Post image not found. Reload the post and try again.", 404
        )
    if not any(
        remote.get(key) not in (None, "")
        for key in ("remote_image_id", "remote_uuid", "remote_url")
    ):
        raise ManualMatchError(
            "post_image_not_remote",
            "This image has no CivitAI identity. Sync the post and try again.",
        )
    if is_video(remote):
        raise ManualMatchError(
            "manual_video_match_unsupported",
            "Videos cannot be matched to local image files.",
        )
    return post, remote


def _owner_image(post: dict[str, Any], remote: dict[str, Any]) -> dict[str, Any] | None:
    remote_post_id = post.get("remote_post_id")
    if not isinstance(remote_post_id, int):
        return None
    try:
        detail = trpc.post_get_edit(remote_post_id)
    except CivitaiError:
        return None
    images = detail.get("images") if isinstance(detail, dict) else None
    if not isinstance(images, list):
        return None

    remote_image_id = remote.get("remote_image_id")
    for image in images:
        if isinstance(image, dict) and image.get("id") == remote_image_id:
            return image
    remote_uuid = str(remote.get("remote_uuid") or "")
    for image in images:
        if isinstance(image, dict) and remote_uuid and str(image.get("url") or "") == remote_uuid:
            return image
    return None


def _download_fingerprints(target: Path, remote: dict[str, Any]) -> dict[str, str | None]:
    # The original, not the downscaled view. Only that is useful for identity
    # hashes, while dHash still tolerates a lossy CivitAI re-encode.
    url = str(remote["remote_url"]).rsplit("/", 1)[0] + "/original=true"
    media.download(url, target)
    return {
        "sha256": hashing.file_sha256(target),
        "pixel_sha256": hashing.pixel_sha256(target),
        "phash": hashing.dhash64(target),
    }


def _phash_candidate_ids(phash: str | None) -> set[int]:
    """Find close-hash candidates with eight indexed equality probes."""
    if not phash:
        return set()
    conn = db.get_connection()
    found: set[int] = set()
    for band, value in enumerate(image_store.phash_bands(phash)):
        if value is None:
            continue
        for row in conn.execute(
            f"SELECT id FROM images WHERE phash_b{band}=?"
            " AND is_missing=0 AND is_trashed=0",
            (value,),
        ):
            found.add(int(row["id"]))
    return found


def _same_name(path: str, remote_name: Any) -> bool:
    if not isinstance(remote_name, str) or not remote_name.strip():
        return False
    local = Path(path).name.casefold()
    remote = Path(remote_name).name.casefold()
    return local == remote or Path(local).stem == Path(remote).stem


def _positive_int(value: Any) -> int | None:
    return value if isinstance(value, int) and value > 0 else None


def _from_history(remote_image_id: int) -> dict[str, Any] | None:
    """Does the usage history know this CivitAI image id?

    Then the hash is there, and the local file is found through it - without a
    single network call.
    """
    row = db.get_connection().execute(
        "SELECT sha256 FROM image_usage WHERE remote_image_id=? AND sha256 != '' LIMIT 1",
        (remote_image_id,),
    ).fetchone()
    if row is None:
        return None
    return _local_by_sha(row["sha256"])


def _local_by_sha(sha256: str) -> dict[str, Any] | None:
    return _local_by_exact("sha256", sha256)


def _by_download(
    folder: Path, row: dict[str, Any], result: dict[str, Any]
) -> dict[str, Any] | None:
    """Fetch the file, hash it, and search over the usual ladder.

    One image that will not download is a per-item failure and counted as such.
    A refused key or a rate limit is not: every remaining row would fail the same
    way, and answering "no local file found" to that would be a lie the user acts
    on. Those come back out and become a 401 or a 429.
    """
    target = folder / f"{row['remote_image_id']}.bin"
    try:
        fingerprints = _download_fingerprints(target, row)
    except (AuthError, RateLimited):
        raise
    except (CivitaiError, OSError, ValueError):
        result["unreadable"] += 1
        return None
    finally:
        target.unlink(missing_ok=True)

    sha = fingerprints["sha256"]
    pixel_sha256 = fingerprints["pixel_sha256"]
    phash = fingerprints["phash"]
    if not sha:
        return None

    exact = _local_by_exact("sha256", sha, remote=row, phash=phash)
    if exact is not None:
        return exact

    # No identical bytes: CivitAI need not hand back the same container, and a
    # later metadata edit moves the byte hash anyway. Exact decoded pixels still
    # identify the local image without guessing among perceptual candidates.
    if not pixel_sha256:
        return None
    return _local_by_exact("pixel_sha256", pixel_sha256, remote=row, phash=phash)


def _local_by_exact(
    field: str,
    value: str,
    *,
    remote: dict[str, Any] | None = None,
    phash: str | None = None,
) -> dict[str, Any] | None:
    """Choose only exact identity, with filename and dHash as ordering hints.

    A CivitAI download often retains its UUID or numeric image id in the local
    filename. That complete token may put an exact candidate first, but can
    never turn a merely similar picture into an automatic link.
    """
    if field not in {"sha256", "pixel_sha256"}:
        raise ValueError(f"Unsupported exact image identity: {field}")
    candidates = [
        dict(candidate)
        for candidate in db.get_connection().execute(
            f"SELECT id, relative_path, phash FROM images WHERE {field}=? AND is_missing=0",
            (value,),
        )
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda candidate: (
            0 if _filename_has_remote_token(candidate["relative_path"], remote) else 1,
            hashing.hamming(phash, candidate.get("phash")),
            candidate["id"],
        )
    )
    return image_store.get(candidates[0]["id"])


def _filename_has_remote_token(path: str, remote: dict[str, Any] | None) -> bool:
    """Whether a complete remote UUID or numeric id is a filename token."""
    if remote is None:
        return False
    filename = Path(path).name
    tokens = (remote.get("remote_uuid"), remote.get("remote_image_id"))
    return any(
        re.search(rf"(?<![A-Za-z0-9]){re.escape(str(token))}(?![A-Za-z0-9])", filename)
        for token in tokens
        if token not in (None, "")
    )


def _link(post_image_id: int, local: dict[str, Any], post_id: int) -> None:
    """Attach the file that was found to the row.

    ``remote_url`` stays: it does no harm, and if the file is lost later the image
    can still be displayed.
    """
    post = post_store.get(post_id)
    published_at = post.get("remote_published_at") if post else None
    remote_post_id = post.get("remote_post_id") if post else None
    is_published_adoption = False
    if post and post.get("origin") == "imported" and isinstance(published_at, str):
        from . import schedule

        published = schedule.parse_iso(published_at)
        is_published_adoption = published is not None and published <= schedule.utcnow()

    with db.transaction() as conn:
        conn.execute(
            "UPDATE post_images SET image_id=?, source_path=?, sha256=?, phash=?,"
            " file_size=?, content_type=?, width=COALESCE(width, ?), height=COALESCE(height, ?)"
            " WHERE id=?",
            (
                local["id"],
                local["absolute_path"],
                local.get("sha256") or "",
                local.get("phash"),
                local.get("file_size"),
                local.get("content_type"),
                local.get("width"),
                local.get("height"),
                post_image_id,
            ),
        )
        remote = conn.execute(
            "SELECT remote_image_id FROM post_images WHERE id=?", (post_image_id,)
        ).fetchone()
        remote_image_id = remote["remote_image_id"] if remote else None
        if (
            is_published_adoption
            and isinstance(remote_post_id, int)
            and isinstance(remote_image_id, int)
        ):
            identity = usage.generation_identity(local.get("parsed"))
            usage.record_or_complete_remote_image(
                sha256=local.get("sha256") or "",
                pixel_sha256=local.get("pixel_sha256"),
                phash=local.get("phash"),
                post_id=post_id,
                remote_post_id=remote_post_id,
                remote_image_id=remote_image_id,
                source_path=local["absolute_path"],
                post_title=post.get("title") or "",
                used_at=published_at,
                status=usage.PUBLISHED,
                origin="match",
                **identity,
                conn=conn,
            )
        else:
            conn.execute(
                "UPDATE image_usage SET post_id=COALESCE(post_id, ?),"
                " source_path=COALESCE(source_path, ?) WHERE remote_image_id=?",
                (post_id, local["absolute_path"], remote_image_id),
            )
