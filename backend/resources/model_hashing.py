"""Hash registered model folders and fill the local CivitAI resource cache."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from .. import hashing, jobs
from ..civitai import rest
from ..civitai.errors import AuthError, RateLimited
from ..store import model_roots as model_store
from . import cache

MODEL_SUFFIXES = frozenset({".bin", ".ckpt", ".gguf", ".pt", ".pth", ".safetensors"})
_MAX_HEADER_BYTES = 100 * 1024 * 1024


def hash_roots(
    job: jobs.Job,
    root_ids: list[int] | None = None,
    *,
    user_requested: bool = False,
) -> dict[str, Any]:
    roots = model_store.list_roots(enabled_only=True)
    if root_ids:
        roots = [root for root in roots if root["id"] in root_ids]

    candidates = _candidates(job, roots)
    candidates.sort(key=lambda item: (item[3].st_size, str(item[1])))
    job.total = len(candidates)
    totals = {"hashed": 0, "skipped": 0, "failed": 0, "forgotten": 0}
    hashes: list[str] = []
    seen: dict[int, set[str]] = {root["id"]: set() for root in roots}

    for root, path, relative_path, stat in candidates:
        if job.should_stop():
            job.result = totals
            return totals
        job.stage = f"Hashing {path.name}"
        # Reconciliation answers whether the path exists, not whether hashing
        # succeeded. A readable directory entry must not be forgotten because
        # one damaged model failed later in this batch.
        seen[root["id"]].add(str(path))
        existing = model_store.get_file(root["id"], path)
        unchanged = (
            existing
            and existing.get("sha256")
            and existing.get("file_mtime") == stat.st_mtime
            and existing.get("file_size") == stat.st_size
        )
        try:
            if unchanged:
                digest = existing["sha256"]
                if not existing.get("file_stem"):
                    names = _read_names(path, stat.st_size)
                    model_store.upsert_file(
                        root["id"],
                        path,
                        relative_path,
                        mtime=stat.st_mtime,
                        size=stat.st_size,
                        sha256=digest,
                        **names,
                    )
                totals["skipped"] += 1
                job.skipped += 1
            else:
                hashed = _hash_and_read_names(path, stat.st_size, job.should_stop)
                if hashed is None:
                    # Cancelled part-way through this file: leave exactly as the
                    # between-files check does, so a cancel never marks a root
                    # hashed or claims a resolution it did not make.
                    job.result = totals
                    return totals
                digest, names = hashed
                model_store.upsert_file(
                    root["id"],
                    path,
                    relative_path,
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    sha256=digest,
                    **names,
                )
                totals["hashed"] += 1
                job.succeeded += 1
        except Exception as exc:
            # One model file must not abort the folder-hashing batch.
            totals["failed"] += 1
            job.failed += 1
            job.log(path=str(path), status="error", message=f"{type(exc).__name__}: {exc}")
            job.processed += 1
            continue
        hashes.append(digest)
        job.processed += 1

    if job.should_stop():
        job.result = totals
        return totals

    # Only for a root that could actually be read: an unmounted drive yields no
    # paths, and forgetting its inventory would mean hashing it all again.
    for root in roots:
        if Path(root["path"]).is_dir():
            totals["forgotten"] += model_store.forget_vanished(root["id"], seen[root["id"]])

    # Model-file requests are bounded by files that are new to the folder, so a
    # quiet watch tick has none. Image-resource requests are bounded by whatever
    # the library still cannot identify, which is a different quantity and does
    # not shrink on its own, so only a user-requested run performs that sweep.
    job.stage = "Resolving model identities"
    lookup = cache.resolve_hashes_bulk(hashes)
    totals["resolved"] = lookup["resolved"]
    totals["unidentified"] = lookup["unknown"]
    totals["cached"] = lookup["cached"]
    totals["failed"] += lookup["failed"]
    job.failed += lookup["failed"]
    model_store.mark_hashed([root["id"] for root in roots if Path(root["path"]).is_dir()])
    totals.update(model_store.identity_counts())

    if user_requested:
        job.stage = "Resolving image resources"
        image_lookup = _resolve_image_resources(job)
        totals["image_resolved"] = image_lookup["resolved"]
        totals["image_unidentified"] = image_lookup["unknown"]
        totals["image_cached"] = image_lookup["cached"]
        totals["image_failed"] = image_lookup["failed"]
        totals["failed"] += image_lookup["failed"]
        job.failed += image_lookup["failed"]
        if image_lookup.get("stopped_early"):
            totals["image_lookup_stopped_early"] = True
            totals["image_lookup_remaining"] = image_lookup["remaining"]
            totals["image_lookup_resumes_next_run"] = True
            if "retry_after" in image_lookup:
                totals["image_lookup_retry_after"] = image_lookup["retry_after"]
    # The projection runs even on a cancel. It is local and costs nothing, and
    # the requests above were already paid for - throwing their answers away
    # because somebody pressed stop afterwards would waste the one part of this
    # job that cannot be repeated for free.
    totals["reapplied"] = cache.reapply_unresolved()
    job.result = totals
    if not job.should_stop():
        job.stage = "Done"
    return totals


def _resolve_image_resources(job: jobs.Job) -> dict[str, Any]:
    """Ask CivitAI for every projectable image-resource hash not already cached."""
    stats = {"resolved": 0, "unknown": 0, "cached": 0, "failed": 0}
    hashes = cache.unresolved_image_hashes()
    full = [value for value in hashes if cache.is_full_sha256(value)]
    partial = [value for value in hashes if value not in full]
    requests = [
        full[start : start + rest.BULK_HASH_LIMIT]
        for start in range(0, len(full), rest.BULK_HASH_LIMIT)
    ]
    requests.extend([value] for value in partial)

    for index, requested in enumerate(requests):
        if job.should_stop():
            break
        try:
            result = (
                cache.resolve_hashes_bulk(requested, stop_on_refusal=True)
                if cache.is_full_sha256(requested[0])
                else cache.resolve_missing(requested, stop_on_refusal=True)
            )
        except (AuthError, RateLimited) as exc:
            # Every remaining request would be refused the same way. Stopping
            # leaves its hashes open for the next Hash Models run.
            remaining = sum(len(batch) for batch in requests[index:])
            stats["stopped_early"] = True
            stats["remaining"] = remaining
            if isinstance(exc, RateLimited) and exc.retry_after is not None:
                stats["retry_after"] = exc.retry_after
            next_step = (
                "run Hash Models again later"
                if isinstance(exc, RateLimited)
                else "reconnect CivitAI, then run Hash Models again"
            )
            job.log(
                status="error",
                message=(
                    f"{type(exc).__name__}: {exc}; {remaining} image-resource hashes "
                    f"remain open; {next_step}."
                ),
            )
            break
        except Exception as exc:
            # One resource lookup must not abort the rest of this explicit batch.
            stats["failed"] += len(requested)
            job.log(
                hash=requested[0] if len(requested) == 1 else None,
                status="error",
                message=f"{type(exc).__name__}: {exc}; run Hash Models again.",
            )
            continue
        for key in stats:
            stats[key] += result[key]
    return stats


def _candidates(
    job: jobs.Job, roots: list[dict[str, Any]]
) -> list[tuple[dict[str, Any], Path, str, os.stat_result]]:
    candidates: list[tuple[dict[str, Any], Path, str, os.stat_result]] = []
    for root in roots:
        if job.should_stop():
            return candidates
        base = Path(root["path"])
        if not base.is_dir():
            job.log(root=root["path"], status="skipped", message="folder not found")
            continue
        for dirpath, dirnames, filenames in os.walk(base, topdown=True, onerror=lambda _: None):
            dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
            for name in sorted(filenames):
                if job.should_stop():
                    return candidates
                path = Path(dirpath) / name
                if path.suffix.lower() not in MODEL_SUFFIXES:
                    continue
                try:
                    stat = path.stat()
                except OSError as exc:
                    job.failed += 1
                    job.log(
                        path=str(path), status="error", message=f"{type(exc).__name__}: {exc}"
                    )
                    continue
                candidates.append((root, path, str(path.relative_to(base)), stat))
    return candidates


def _hash_and_read_names(
    path: Path, file_size: int, should_stop: Callable[[], bool] | None = None
) -> tuple[str, dict[str, str | None]] | None:
    """Hash one model file. ``None`` when the caller asked to stop part-way.

    Checked inside the read loop, not only between files: a checkpoint runs to
    thirteen gigabytes here, and a cancel that waits for it is not a cancel.
    """
    digest = hashlib.sha256()
    metadata: dict[str, Any] = {}
    with path.open("rb") as handle:
        prefix = handle.read(8)
        digest.update(prefix)
        if path.suffix.lower() == ".safetensors":
            metadata = _read_safetensors_metadata(handle, prefix, file_size, digest.update)
        for chunk in iter(lambda: handle.read(hashing.CHUNK), b""):
            if should_stop is not None and should_stop():
                return None
            digest.update(chunk)
    return digest.hexdigest().upper(), _model_names(path, metadata)


def _read_names(path: Path, file_size: int) -> dict[str, str | None]:
    metadata: dict[str, Any] = {}
    if path.suffix.lower() == ".safetensors":
        with path.open("rb") as handle:
            prefix = handle.read(8)
            metadata = _read_safetensors_metadata(handle, prefix, file_size)
    return _model_names(path, metadata)


def _read_safetensors_metadata(
    handle: BinaryIO,
    prefix: bytes,
    file_size: int,
    consume: Callable[[bytes], object] | None = None,
) -> dict[str, Any]:
    if len(prefix) != 8:
        return {}
    header_size = int.from_bytes(prefix, "little")
    if header_size < 2 or header_size > min(_MAX_HEADER_BYTES, file_size - 8):
        return {}
    raw_header = handle.read(header_size)
    if consume is not None:
        consume(raw_header)
    try:
        header = json.loads(raw_header)
    except (UnicodeDecodeError, ValueError):
        return {}
    metadata = header.get("__metadata__") if isinstance(header, dict) else None
    return metadata if isinstance(metadata, dict) else {}


def _model_names(path: Path, metadata: dict[str, Any]) -> dict[str, str | None]:
    def text_value(key: str) -> str | None:
        value = metadata.get(key)
        return value if isinstance(value, str) and value else None

    return {
        "file_stem": path.stem,
        "ss_output_name": text_value("ss_output_name"),
        "modelspec_title": text_value("modelspec.title"),
    }
