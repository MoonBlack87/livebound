"""The three optional checks the maintainer can switch on.

Each is a plain function that does **one pass** and returns. The periodicity
belongs to `scheduler.py`; nothing here knows when it runs.

**Everything is off until it is switched on.** A fresh installation and a
restored backup behave exactly as they did before: every check asks its setting
first and returns without touching anything.

Two lines are not negotiable and are repeated at the sites that could cross
them:

- `ARC-04` - archiving happens only on an explicit press. **No check here ever
  moves a file.** The post check reads and writes post state; the folder checks
  start the scan that already exists.
- `LIF-11` - a divergence between CivitAI and the local state is offered to the
  maintainer as a choice, never resolved automatically. A check that finds one
  **stops there**; the divergence flag `sync_one` already sets is what surfaces
  it in the interface.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from typing import Any

from . import db, jobs

#: Global switch for the post check. Off unless it says "1".
SETTING_WATCH_POSTS = "watch_posts_enabled"

#: How long after each unsuccessful check to look again. The initial check plus
#: these three retries is four checks in all. This ladder belongs only to the
#: publication fact. Ratings have their own continuing cadence below because a
#: rating can arrive early and can still change later.
RETRY_DELAYS_SECONDS = (5 * 60, 15 * 60, 45 * 60)
MAX_POST_CHECKS = len(RETRY_DELAYS_SECONDS) + 1

#: Once every image has a rating, keep reading it while the post remains on the
#: board, but less often than while any rating is absent. The post watcher runs
#: once a minute, so fifteen minutes is an exact scheduler window.
RATING_REFRESH_SECONDS = 15 * 60

#: A locally forgotten post that still exists remotely is checked periodically,
#: not on every minute tick. The interval is fixed with the rest of the checks.
ORPHAN_REFRESH_SECONDS = 15 * 60

#: Bound local posts and orphaned remote posts independently in every pass.
#: Each post normally costs at most two CivitAI reads, including after a restart.
POST_CHECK_BATCH_SIZE = 10

#: Per-post retry bookkeeping, in memory on purpose: a restart re-checks from
#: scratch, which is correct and cheaper than a column for a case that lasts
#: minutes.
_attempts: dict[int, int] = {}
_next_attempt_at: dict[int, float] = {}
_last_rating_check_at: dict[int, float] = {}
_last_orphan_check_at: dict[int, float] = {}


def reset_state() -> None:
    """Forget all retry bookkeeping after an explicit reset or process change."""
    _attempts.clear()
    _next_attempt_at.clear()
    _last_rating_check_at.clear()
    _last_orphan_check_at.clear()


def reset_post(post_id: int) -> None:
    """Reset one post's publication retry and rating refresh schedule."""
    _attempts.pop(post_id, None)
    _next_attempt_at.pop(post_id, None)
    _last_rating_check_at.pop(post_id, None)


def _enabled(setting: str) -> bool:
    # This lookup sits on the background-startup boundary. It must never create
    # or migrate the database whose location first-start setup is choosing.
    return (db.get_setting_read_only(setting) or "") == "1"


def _publish_time_has_passed(post: dict[str, Any], cutoff: str) -> bool:
    """Whether this remote post is at a point where publication can be missing."""
    if post.get("state") not in {"scheduled", "published"}:
        return False
    publish_time = post.get("remote_published_at") or post.get("scheduled_at")
    return bool(publish_time and publish_time <= cutoff)


def missing_facts(
    post: dict[str, Any],
    images: list[dict[str, Any]],
    *,
    cutoff: str | None = None,
) -> list[str]:
    """Facts CivitAI can meaningfully have supplied by this point.

    Deliberately short and checkable. Two things only:

    - ``published`` - only after the post's publish time has passed;
    - ``rating`` - CivitAI's content rating for every image it holds. This is
      meaningful as soon as an image has been uploaded, and can still change
      after the first value arrives.

    Nothing else. A longer list would turn a narrow re-check into a general
    poller, and there is no third fact this application can decide is "missing"
    rather than simply absent.
    """
    if cutoff is None:
        from .posts import schedule

        cutoff = schedule.iso_z(schedule.utcnow())

    missing = []
    if post.get("state") != "published" and _publish_time_has_passed(post, cutoff):
        missing.append("published")
    if any(
        image.get("remote_image_id") and not image.get("nsfw_level") for image in images
    ):
        missing.append("rating")
    return missing


def anything_watched() -> bool:
    """Is any of the three checks switched on?"""
    from .store import model_roots as model_store
    from .store import sources as source_store

    if _enabled(SETTING_WATCH_POSTS):
        return True
    return any(root.get("watch_enabled") for root in source_store.list_roots()) or any(
        root.get("watch_enabled") for root in model_store.list_roots()
    )


def check_due_posts(now: Callable[[], float] = time.monotonic) -> dict[str, Any]:
    """Read publication state and changing ratings for remote posts on the board.

    `LIF-11`: this only calls ``sync_one``, which records what CivitAI holds and
    raises the divergence flag. It never picks a side, and it never resolves a
    divergence it finds. `ARC-04`: no file is touched here at all.
    """
    if not _enabled(SETTING_WATCH_POSTS):
        return {"checked": 0, "skipped": "off"}

    from .posts import lifecycle, schedule, sync
    from .store import posts as post_store
    from .store import usage

    cutoff = schedule.iso_z(schedule.utcnow())
    moment = now()
    orphan_ids = usage.orphaned_remote_post_ids()
    active_orphan_ids = set(orphan_ids)
    for remote_post_id in _last_orphan_check_at.keys() - active_orphan_ids:
        _last_orphan_check_at.pop(remote_post_id, None)
    for remote_post_id in active_orphan_ids - _last_orphan_check_at.keys():
        _last_orphan_check_at[remote_post_id] = moment - ORPHAN_REFRESH_SECONDS
    orphan_wanted = [
        remote_post_id
        for remote_post_id in orphan_ids
        if moment >= _last_orphan_check_at[remote_post_id] + ORPHAN_REFRESH_SECONDS
    ]
    orphan_wanted.sort(
        key=lambda remote_post_id: (
            _last_orphan_check_at[remote_post_id] + ORPHAN_REFRESH_SECONDS,
            remote_post_id,
        )
    )
    orphan_wanted = orphan_wanted[:POST_CHECK_BATCH_SIZE]
    missing_rating_ids = {
        row["post_id"]
        for row in db.get_connection().execute(
            "SELECT DISTINCT post_id FROM post_images"
            " WHERE remote_image_id IS NOT NULL"
            " AND (nsfw_level IS NULL OR nsfw_level=0)"
        )
    }
    # Paged: `list_posts` caps at 500 by default, and a library with more
    # published posts than that would have silently stopped checking the rest.
    candidates: list[dict[str, Any]] = []
    offset = 0
    while True:
        page = post_store.list_posts(
            states=[
                lifecycle.REMOTE_DRAFT,
                lifecycle.SCHEDULED,
                lifecycle.PUBLISHED,
            ],
            limit=500,
            offset=offset,
        )
        candidates.extend(post for post in page if post.get("remote_post_id"))
        if len(page) < 500:
            break
        offset += len(page)

    active_ids = {post["id"] for post in candidates}
    for post_id in _attempts.keys() - active_ids:
        _attempts.pop(post_id, None)
        _next_attempt_at.pop(post_id, None)
    for post_id in _last_rating_check_at.keys() - active_ids:
        _last_rating_check_at.pop(post_id, None)

    eligible: list[tuple[int, float, dict[str, Any], bool]] = []
    for post in candidates:
        post_id = post["id"]
        rating_missing = post_id in missing_rating_ids
        rating_interval = POST_CHECK_SECONDS if rating_missing else RATING_REFRESH_SECONDS
        last_rating_check = _last_rating_check_at.get(post_id)
        rating_due = last_rating_check is None or moment >= last_rating_check + rating_interval
        publication_due = (
            _publish_time_has_passed(post, cutoff)
            and _attempts.get(post_id, 0) < MAX_POST_CHECKS
            and moment >= _next_attempt_at.get(post_id, 0.0)
        )
        if rating_due or publication_due:
            # Missing ratings first, then a due publication, then the slower
            # refresh of ratings already present. The oldest rating read wins
            # within a group so the ten-post bound cannot starve later posts.
            priority = 0 if rating_missing else 1 if publication_due else 2
            eligible.append(
                (
                    priority,
                    last_rating_check or 0.0,
                    post,
                    publication_due,
                )
            )

    eligible.sort(
        key=lambda value: value[2].get("remote_published_at")
        or value[2].get("scheduled_at")
        or "",
        reverse=True,
    )
    eligible.sort(key=lambda value: (value[0], value[1]))
    wanted = eligible[:POST_CHECK_BATCH_SIZE]
    if not wanted and not orphan_wanted:
        return {"checked": 0}

    def run(job: jobs.Job) -> None:
        totals = {
            "changed": 0,
            "divergences": 0,
            "ratings_updated": 0,
            "released": 0,
        }
        job.total = len(wanted) + len(orphan_wanted)
        job.stage_code = "posts"
        job.stage = "Checking CivitAI post state, image ratings, and usage history"
        for _, _, post, publication_due in wanted:
            post_id = post["id"]
            job.processed += 1
            # `LIF-11`: never overwrite local intent automatically. The default
            # would write CivitAI's `hideMeta` over the maintainer's, which is
            # "fetch from CivitAI and overwrite locally" without being asked.
            result = sync.sync_one(post_id, preserve_hide_meta=True)
            checked_at = now()
            _last_rating_check_at[post_id] = checked_at
            if isinstance(result, dict):
                for key in totals:
                    totals[key] += int(result.get(key, 0))
                if result.get("status") == "error":
                    job.failed += 1
                    message = result.get("message") or "Try the CivitAI check again."
                    job.log(
                        post_id=post_id,
                        status="error",
                        message=f"CivitAI read failed: {message}",
                    )
                    continue
            fresh = post_store.get(post_id)
            if fresh is None:
                continue
            fresh_images = post_store.images(post_id)
            missing = missing_facts(fresh, fresh_images, cutoff=cutoff)
            if publication_due and "published" not in missing:
                # Publication and rating settle independently. A rating that is
                # still absent must not keep the publication ladder alive.
                _attempts[post_id] = MAX_POST_CHECKS
                _next_attempt_at.pop(post_id, None)
            if not missing:
                job.succeeded += 1
                job.log(post_id=post_id, status="ok", message="complete")
                continue

            messages = []
            log_status = "info"
            if "rating" in missing:
                messages.append("rating missing; checking again next minute")
            if "published" in missing and publication_due:
                attempt = _attempts.get(post_id, 0)
                _attempts[post_id] = attempt + 1
                if attempt + 1 >= MAX_POST_CHECKS:
                    messages.append(
                        f"publication still missing after {attempt + 1} checks"
                    )
                    log_status = "error"
                    job.failed += 1
                else:
                    _next_attempt_at[post_id] = (
                        checked_at + RETRY_DELAYS_SECONDS[attempt]
                    )
                    messages.append("publication missing; looking again later")
            if messages:
                job.log(
                    post_id=post_id,
                    status=log_status,
                    message="; ".join(messages),
                )
        for remote_post_id in orphan_wanted:
            job.processed += 1
            result = sync.check_orphaned_usage(remote_post_id)
            _last_orphan_check_at[remote_post_id] = now()
            totals["released"] += int(result.get("released", 0))
            if result["status"] == "error":
                job.failed += 1
                message = result.get("message") or "Try the CivitAI check again."
                job.log(
                    remote_post_id=remote_post_id,
                    status="error",
                    message=f"CivitAI read failed: {message}",
                )
            elif result["status"] == "missing_remote":
                released = int(result.get("released", 0))
                job.succeeded += 1
                job.log(
                    remote_post_id=remote_post_id,
                    status="ok",
                    message=f"post missing; {released} image usage row(s) released",
                )
            else:
                job.succeeded += 1
                job.log(
                    remote_post_id=remote_post_id,
                    status="ok",
                    message="post still exists; images remain held",
                )
        job.result = totals

    # The job id is returned so a caller can wait for the pass to finish - the
    # bookkeeping below is updated on the worker thread, not here.
    try:
        job = jobs.start("watch-posts", run, exclusive="watch-posts")
    except jobs.AlreadyRunning:
        # The previous pass outlived the interval. Nothing to add.
        return {"checked": 0, "skipped": "running"}
    return {"checked": len(wanted) + len(orphan_wanted), "job": job.id}


def check_source_roots() -> dict[str, Any]:
    """Scan the source roots that were switched on.

    A watcher does not gain its own ingestion path: this is the scan that
    already exists, started the way a press starts it.
    """
    from .store import sources as source_store

    watched = [root for root in source_store.list_roots() if root.get("watch_enabled")]
    if not watched:
        return {"roots": 0}

    from .scanner import service

    watched_ids = [root["id"] for root in watched]
    try:
        job = jobs.start(
            "scan",
            # Only the folders that were switched on, and no conclusions about
            # files that disappeared - that stays with a pressed scan.
            lambda job: service.scan_roots(job, watched_ids, reconcile_missing=False),
            exclusive="scan",
        )
    except jobs.AlreadyRunning:
        # Someone is already scanning. Nothing to add.
        return {"roots": 0, "skipped": "running"}
    return {"roots": len(watched), "job": job.id}


def check_model_roots() -> dict[str, Any]:
    """Hash the model roots that were switched on. Same rule as above."""
    from .store import model_roots as model_store

    watched = [root for root in model_store.list_roots() if root.get("watch_enabled")]
    if not watched:
        return {"roots": 0}

    from .resources import model_hashing

    watched_ids = [root["id"] for root in watched]
    try:
        job = jobs.start(
            "model-hash",
            lambda job: model_hashing.hash_roots(job, watched_ids),
            exclusive="model-hash",
        )
    except jobs.AlreadyRunning:
        return {"roots": 0, "skipped": "running"}
    return {"roots": len(watched), "job": job.id}


#: How often each check runs. Fixed, not configurable - `SCO-06`.
POST_CHECK_SECONDS = 60.0
FOLDER_CHECK_SECONDS = 15 * 60.0


#: How often the daily backup is *considered*. `create_regular_if_due` decides
#: whether one is actually due, so the interval only has to be short enough that
#: an installation left running for weeks does not skip a day - not a second
#: schedule of its own (`SCO-06`).
BACKUP_CHECK_SECONDS = 60 * 60.0


def check_backup_due() -> dict[str, Any]:
    """Take the daily backup if one is due.

    It used to happen only in `init_db`, which means only at startup. An
    installation that is simply left running went without one for as long as it
    ran, while the settings text promised a backup every day. The decision stays
    where it was; what changed is that something asks.
    """
    from . import backup

    try:
        created = backup.create_regular_if_due()
    except (OSError, sqlite3.Error) as exc:
        # A missed precaution must not take the scheduler down with it; the next
        # hour asks again, and Settings can always take one by hand.
        return {"created": 0, "error": str(exc)}
    return {"created": 1 if created else 0}


def register(scheduler: Any) -> None:
    """Put the checks on the scheduler. They gate themselves on their setting."""
    scheduler.every("watch-posts", POST_CHECK_SECONDS, check_due_posts)
    scheduler.every("watch-sources", FOLDER_CHECK_SECONDS, check_source_roots)
    scheduler.every("watch-models", FOLDER_CHECK_SECONDS, check_model_roots)
    scheduler.every("backup-due", BACKUP_CHECK_SECONDS, check_backup_due)
