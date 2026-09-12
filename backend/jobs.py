"""One registry for every long-running task: scan, push, sync, LLM.

The app deliberately has no websockets and no task queue. A job is a plain
dataclass in a module-level dict, mutated by a daemon thread and polled by the
UI over ``GET /api/jobs/{id}``. That is enough for a single-user local tool and
it keeps the failure modes obvious.

Progress lives in memory, not in the database - only durable state transitions
are written, so a chatty progress bar never contends for the SQLite write lock.
"""

from __future__ import annotations

import contextlib
import threading
import time
import traceback
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_jobs: dict[int, Job] = {}
_lock = threading.Lock()
_next_id = 1
_filesystem_threads: dict[int, threading.Thread] = {}

#: Every worker job that moves or writes files outside the database belongs
#: here. Other jobs retain the usual daemon-thread behaviour and may stop with
#: the process.
FILESYSTEM_MUTATING_KINDS = frozenset(
    {"archive", "archive-backup", "fetch", "move-data", "trash"}
)

#: Finished jobs are kept so the UI can still read the final result after the
#: last poll; past this many, the oldest finished ones are dropped.
MAX_RETAINED = 50


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class Job:
    id: int
    kind: str
    exclusive: str | None = None
    status: str = "starting"          # starting | running | done | error | cancelled
    stage: str = ""
    #: A stable name for the current stage, so the frontend can say it in
    #: the user's language. ``stage`` stays the English text.
    stage_code: str = ""
    total: int = 0
    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    items: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=_now)
    finished_at: str | None = None
    _cancelled: bool = False

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def cancel(self) -> None:
        self._cancelled = True

    def should_stop(self) -> bool:
        return self._cancelled

    #: Kept in memory, so it is bounded rather than trimmed only on the wire -
    #: a scan over a folder with ten thousand unreadable files would otherwise
    #: accumulate ten thousand dicts for a list nobody reads past the last 50.
    MAX_ITEMS = 200

    def log(self, **entry: Any) -> None:
        """Append one item outcome. The wire only ever carries the newest few."""
        self.items.append({"at": _now(), **entry})
        if len(self.items) > self.MAX_ITEMS:
            del self.items[: len(self.items) - self.MAX_ITEMS]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "stage": self.stage,
            "stage_code": self.stage_code,
            "total": self.total,
            "processed": self.processed,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "error": self.error,
            "result": self.result,
            # trimmed: a batch of 200 posts must not send 200 rows on every poll
            "items": self.items[-50:],
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class AlreadyRunning(RuntimeError):
    """A job of this kind is already going. Carries the one that is."""

    def __init__(self, job: Job):
        super().__init__(f"job {job.id} ({job.kind}) is already running")
        self.job = job


def start(
    kind: str, target: Callable[[Job], None], *, exclusive: str | None = None
) -> Job:
    """Run ``target`` on a daemon thread and return the job immediately.

    ``exclusive`` refuses to start when a job of that kind - or, given ``"*"``,
    of any kind - is already running. An existing job's own scope is checked in
    the other direction too. The check happens under the same lock as the
    insert, because asking first and starting afterwards is two steps: two
    requests can both see nothing running and both start, and two archive runs
    moving the same files at once is not a situation to leave to timing.
    """
    def runner() -> None:
        from . import db

        job.status = "running"
        try:
            target(job)
            if job.status == "running":
                job.status = "cancelled" if job.cancelled else "done"
        except Exception as exc:
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        finally:
            job.finished_at = _now()
            # worker threads get their own sqlite connection; give it back
            db.close_connection()
            if kind in FILESYSTEM_MUTATING_KINDS:
                with _lock:
                    _filesystem_threads.pop(job.id, None)

    global _next_id
    with _lock:
        running = _active_locked(kind, exclusive)
        if running is not None:
            raise AlreadyRunning(running)
        job = Job(id=_next_id, kind=kind, exclusive=exclusive)
        _next_id += 1
        _jobs[job.id] = job
        _prune_locked()
        thread = threading.Thread(
            target=runner, daemon=True, name=f"job-{job.id}-{kind}"
        )
        if kind in FILESYSTEM_MUTATING_KINDS:
            _filesystem_threads[job.id] = thread
        thread.start()
    return job


@contextlib.contextmanager
def reserve(kind: str, *, exclusive: str = "*") -> Iterator[Job]:
    """Hold a job slot for work that runs *inside* the request, not on a thread.

    A restore replaces the whole database while the caller waits for the answer.
    With the default ``"*"`` scope, nothing else may start while it runs, and an
    existing job of any kind prevents the reservation. The check and insert use
    the same lock, so neither direction leaves a race window.
    """
    global _next_id
    with _lock:
        running = _active_locked(kind, exclusive)
        if running is not None:
            raise AlreadyRunning(running)
        job = Job(id=_next_id, kind=kind, exclusive=exclusive, status="running")
        _next_id += 1
        _jobs[job.id] = job
        _prune_locked()
    try:
        yield job
        if job.status == "running":
            job.status = "done"
    except Exception as exc:
        job.status = "error"
        job.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        job.finished_at = _now()


def get(job_id: int) -> Job | None:
    with _lock:
        return _jobs.get(job_id)


def _active_locked(kind: str, exclusive: str | None) -> Job | None:
    for job in _jobs.values():
        if job.status not in ("starting", "running"):
            continue
        if (
            exclusive == "*"
            or exclusive == job.kind
            or job.exclusive == "*"
            or job.exclusive == kind
        ):
            return job
    return None


def active(kind: str | None = None) -> Job | None:
    """The running job of this kind, if any.

    Fine for reporting. To *refuse* a concurrent run, pass ``exclusive`` to
    :func:`start` instead - asking here and starting afterwards leaves a gap
    between the two.
    """
    with _lock:
        return next(
            (
                job
                for job in _jobs.values()
                if job.status in ("starting", "running")
                and (kind is None or job.kind == kind)
            ),
            None,
        )


def updates(after: int | None = None) -> tuple[list[Job], int]:
    """Jobs a poll has not seen yet, plus the cursor for its next poll.

    With no cursor, establish a baseline while still reporting work that is
    running now. Once the browser has that baseline, every job created after it
    is returned even if it finished between two polls. The registry keeps the
    last finished jobs precisely so this does not depend on lucky timing.

    A browser can outlive a server restart, while job ids cannot. An impossible
    cursor therefore starts a fresh baseline instead of hiding new jobs until
    the process happens to count past the old id again.
    """
    with _lock:
        cursor = max(_jobs, default=0)
        if after is None or after > cursor:
            selected = [
                job for job in _jobs.values() if job.status in ("starting", "running")
            ]
        else:
            selected = [job for job_id, job in _jobs.items() if job_id > after]
        return sorted(selected, key=lambda job: job.id), cursor


def wait_for_filesystem_jobs(timeout: float) -> list[Job]:
    """Wait up to ``timeout`` seconds for file-mutating worker jobs to finish.

    The shared deadline bounds the whole shutdown even if several such jobs are
    active. Any jobs returned are still running when that deadline expired.
    """
    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        with _lock:
            active_threads = [
                (job_id, thread)
                for job_id, thread in _filesystem_threads.items()
                if thread.is_alive()
            ]
        if not active_threads:
            return []

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            with _lock:
                return [
                    _jobs[job_id]
                    for job_id, thread in _filesystem_threads.items()
                    if thread.is_alive() and job_id in _jobs
                ]
        active_threads[0][1].join(remaining)


def all_jobs() -> list[Job]:
    # Under the lock: a job starting while this iterates would otherwise raise
    # "dictionary changed size during iteration" at whoever asked for the list.
    with _lock:
        return sorted(_jobs.values(), key=lambda j: j.id, reverse=True)


def _prune_locked() -> None:
    finished = [j for j in _jobs.values() if j.status in ("done", "error", "cancelled")]
    if len(finished) <= MAX_RETAINED:
        return
    for job in sorted(finished, key=lambda j: j.id)[: len(finished) - MAX_RETAINED]:
        _jobs.pop(job.id, None)
