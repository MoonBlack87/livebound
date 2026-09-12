"""The one place that owns "run this again later".

Deliberately small. One daemon thread wakes on a fixed heartbeat, looks at a
list of due entries and runs them; that is the whole mechanism. There is no cron
expression, no rule engine, no configurable interval and no one-shot, because
nobody has asked for any of them and `SCO-06` says not to build for a case that
does not exist. If a fourth check is wanted tomorrow it is one more `every()`
call.

**Responsibilities stay apart.** This module knows *when*, never *what*: it
imports nothing from `posts/`, `scanner/` or `store/`. The checks live with
their domain and are plain functions that do one pass; `main.py` registers them.

The clock is injectable so a test can drive time forward without sleeping. A
scheduler tested against real time is slow and flaky and proves less.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

_logger = logging.getLogger(__name__)

#: How often the thread wakes. Everything here is minutes-scale, so a coarse
#: heartbeat is enough and keeps an idle installation quiet.
TICK_SECONDS = 30.0


@dataclass
class _Entry:
    name: str
    run: Callable[[], None]
    due_at: float
    #: The gap until the next run.
    every: float


@dataclass
class Scheduler:
    """A list of things to run later, and a thread that runs them."""

    clock: Callable[[], float] = time.monotonic
    tick_seconds: float = TICK_SECONDS
    _entries: list[_Entry] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None

    def every(self, name: str, seconds: float, run: Callable[[], None]) -> None:
        """Run ``run`` every ``seconds``, starting one interval from now."""
        with self._lock:
            self._entries = [entry for entry in self._entries if entry.name != name]
            self._entries.append(
                _Entry(name, run, due_at=self.clock() + seconds, every=seconds)
            )

    def pending(self) -> list[str]:
        with self._lock:
            return sorted(entry.name for entry in self._entries)

    def tick(self) -> int:
        """Run everything that is due. Returns how many ran.

        Public so a test can drive it directly with its own clock. One entry
        that raises must not stop the others or kill the thread - this is
        background upkeep, not the point of anyone's click.
        """
        now = self.clock()
        with self._lock:
            due = [entry for entry in self._entries if entry.due_at <= now]
            for entry in due:
                entry.due_at = now + entry.every

        for entry in due:
            try:
                entry.run()
            except Exception:
                # One failing check must not take the scheduler down with it.
                _logger.exception("Scheduled check %s failed", entry.name)
        return len(due)

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        """Stop with the application, the way the job threads already do."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout)

    def _loop(self) -> None:
        while not self._stop.wait(self.tick_seconds):
            self.tick()


#: The process-wide instance. One scheduler, so "when" has a single owner.
scheduler = Scheduler()
