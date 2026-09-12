"""The optional background checks, and the promise that they are optional.

Every test here drives a clock it owns. A scheduler tested against real time is
slow, flaky, and proves less than one that can jump forty-five minutes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from backend import db, jobs, scheduler, watchers
from backend.civitai.errors import TransportError
from backend.posts import lifecycle
from backend.store import model_roots as model_store
from backend.store import posts as post_store
from backend.store import sources as source_store


class Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _forget_retries():
    watchers.reset_state()
    yield
    watchers.reset_state()


def test_a_fresh_installation_runs_nothing_by_itself(client, scanned):
    """Off by default, everywhere. The whole point of the feature's contract."""
    clock = Clock()
    board = scheduler.Scheduler(clock=clock)
    watchers.register(board)

    post = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()
    post_store.set_fields(
        post["id"],
        state=lifecycle.SCHEDULED,
        remote_post_id=4242,
        remote_published_at="2020-01-01T00:00:00Z",
    )
    before = len(jobs.all_jobs())

    clock.advance(24 * 60 * 60)
    # Four entries run; three of them are the watches and do nothing without
    # their setting. The fourth is the daily backup, which is deliberately not
    # gated on anything - it is this application's own safety net, not something
    # it does to the user's folders, and the settings text has always promised
    # it daily. It starts no job either, which is what this test is guarding.
    assert board.tick() == 4, "every registered check ran"

    assert post_store.get(post["id"])["state"] == lifecycle.SCHEDULED, "nothing synced"
    assert client.get("/api/settings").json()["watch_posts_enabled"] is False
    assert len(jobs.all_jobs()) == before, "no job was started"


def test_the_daily_backup_is_taken_while_the_application_simply_runs(client):
    """It used to happen only at startup, so a long run went without one.

    The decision about whether one is due has not moved; what changed is that
    something asks while the process is alive.
    """
    from backend import backup

    asked: list[bool] = []

    def create_regular_if_due(**_: object) -> None:
        asked.append(True)
        return None

    original = backup.create_regular_if_due
    backup.create_regular_if_due = create_regular_if_due
    try:
        clock = Clock()
        board = scheduler.Scheduler(clock=clock)
        watchers.register(board)
        clock.advance(watchers.BACKUP_CHECK_SECONDS + 1)
        board.tick()
    finally:
        backup.create_regular_if_due = original

    assert asked == [True], "the scheduler asked whether a backup is due"


def test_an_entry_comes_back_every_interval_and_not_before():
    clock = Clock()
    board = scheduler.Scheduler(clock=clock)
    runs = []
    board.every("beat", 60, lambda: runs.append("beat"))

    assert board.tick() == 0, "nothing is due yet"
    clock.advance(60)
    assert board.tick() == 1
    assert board.tick() == 0, "and not twice for one interval"
    clock.advance(60)
    board.tick()

    assert runs == ["beat", "beat"]
    assert board.pending() == ["beat"]


def test_one_failing_check_does_not_stop_the_others():
    """Background upkeep must not take the scheduler down with it."""
    clock = Clock()
    board = scheduler.Scheduler(clock=clock)
    runs = []

    def boom() -> None:
        raise RuntimeError("no")

    board.every("bad", 10, boom)
    board.every("good", 10, lambda: runs.append("good"))
    clock.advance(10)

    assert board.tick() == 2
    assert runs == ["good"]
    clock.advance(10)
    assert board.tick() == 2, "the failing one is still scheduled"


def test_every_retry_delay_is_applied_before_the_check_gives_up(
    client, scanned, monkeypatch
):
    """Publication gets four checks; the separate rating refresh continues."""
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    clock = Clock()

    post = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()
    post_id = post["id"]
    post_store.set_fields(
        post_id,
        state=lifecycle.SCHEDULED,
        remote_post_id=4242,
        remote_published_at="2020-01-01T00:00:00Z",
    )

    # Keep ratings complete so this test isolates the publication ladder.
    with db.transaction() as conn:
        for row in post_store.images(post_id):
            conn.execute(
                "UPDATE post_images SET remote_image_id=?, nsfw_level=? WHERE id=?",
                (900 + row["position"], 2, row["id"]),
            )

    synced = []
    from backend.posts import sync

    monkeypatch.setattr(sync, "sync_one", lambda post_id, **kw: synced.append(post_id))

    # The stand-in leaves the due post scheduled, so "published" stays missing.
    _finish(watchers.check_due_posts(now=clock))
    # Hold the independent rating refresh out of this publication-only proof.
    watchers._last_rating_check_at[post_id] = clock() + 24 * 60 * 60
    for expected_delay in watchers.RETRY_DELAYS_SECONDS:
        clock.advance(expected_delay - 1)
        assert watchers.check_due_posts(now=clock) == {"checked": 0}
        clock.advance(1)
        _finish(watchers.check_due_posts(now=clock))
        watchers._last_rating_check_at[post_id] = clock() + 24 * 60 * 60

    assert len(synced) == 4, "the initial check and all three retries ran"

    clock.advance(watchers.POST_CHECK_SECONDS)
    assert watchers.check_due_posts(now=clock) == {"checked": 0}, "no fifth retry"
    assert len(synced) == 4
    assert watchers._attempts[post_id] == watchers.MAX_POST_CHECKS

    watchers._last_rating_check_at[post_id] = clock()
    clock.advance(watchers.RATING_REFRESH_SECONDS)
    _finish(watchers.check_due_posts(now=clock))
    assert len(synced) == 5, "the independent rating refresh still ran"
    assert watchers._attempts[post_id] == watchers.MAX_POST_CHECKS


def test_switching_the_post_check_off_and_on_clears_the_retry_cap(
    client, scanned, monkeypatch
):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    post_id = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    post_store.set_fields(
        post_id,
        state=lifecycle.SCHEDULED,
        remote_post_id=4242,
        remote_published_at="2020-01-01T00:00:00Z",
    )
    watchers._attempts[post_id] = watchers.MAX_POST_CHECKS

    disabled = client.put("/api/settings", json={"watch_posts_enabled": False}).json()
    assert post_id not in watchers._attempts
    assert disabled["watch_post_retry_minutes"] == [5, 15, 45]
    assert disabled["watch_post_rating_refresh_minutes"] == 15
    client.put("/api/settings", json={"watch_posts_enabled": True})

    synced = []
    from backend.posts import sync

    monkeypatch.setattr(sync, "sync_one", lambda post_id, **kw: synced.append(post_id))
    _finish(watchers.check_due_posts())
    assert synced == [post_id]


def test_a_manual_post_sync_clears_its_retry_cap(client, scanned, monkeypatch):
    post_id = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    post_store.set_fields(post_id, remote_post_id=4242)
    watchers._attempts[post_id] = watchers.MAX_POST_CHECKS
    watchers._next_attempt_at[post_id] = 999999.0

    from backend.posts import sync

    monkeypatch.setattr(sync, "sync_one", lambda post_id: {"status": "scheduled"})
    response = client.post(f"/api/posts/{post_id}/sync")

    assert response.status_code == 200
    assert post_id not in watchers._attempts
    assert post_id not in watchers._next_attempt_at


def test_a_restart_checks_at_most_ten_due_posts_in_its_first_minute(
    client, monkeypatch
):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    for index in range(100):
        post_id = post_store.create(
            title=f"Historical {index}",
            state=lifecycle.PUBLISHED,
            scheduled_at="2020-01-01T00:00:00Z",
        )
        post_store.set_fields(
            post_id,
            remote_post_id=5000 + index,
            remote_published_at="2020-01-01T00:00:00Z",
        )

    # Empty bookkeeping is the cold-start state. The pass itself is the bound:
    # ten posts, normally two CivitAI reads each, rather than two hundred calls.
    watchers.reset_state()
    synced = []
    from backend.posts import sync

    monkeypatch.setattr(sync, "sync_one", lambda post_id, **kw: synced.append(post_id))
    result = watchers.check_due_posts()
    _finish(result)

    assert result["checked"] == 10
    assert len(synced) == 10


def test_a_future_scheduled_post_is_checked_until_its_rating_arrives(
    client, scanned, monkeypatch
):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    clock = Clock()
    post_id = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    post_store.set_fields(
        post_id,
        state=lifecycle.SCHEDULED,
        remote_post_id=4242,
        remote_published_at="2999-01-01T00:00:00Z",
    )
    image = post_store.images(post_id)[0]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE post_images SET remote_image_id=?, nsfw_level=NULL WHERE id=?",
            (900, image["id"]),
        )

    synced = []
    from backend.posts import sync

    def supply_rating(synced_post_id, **kwargs):
        synced.append((synced_post_id, kwargs))
        with db.transaction() as conn:
            conn.execute(
                "UPDATE post_images SET nsfw_level=? WHERE id=?", (8, image["id"])
            )

    monkeypatch.setattr(sync, "sync_one", supply_rating)
    result = watchers.check_due_posts(now=clock)

    assert result["checked"] == 1
    _finish(result)
    assert synced == [(post_id, {"preserve_hide_meta": True})]
    assert post_store.images(post_id)[0]["nsfw_level"] == 8
    assert post_id not in watchers._attempts, "publication is not due yet"

    clock.advance(watchers.POST_CHECK_SECONDS)
    assert watchers.check_due_posts(now=clock) == {"checked": 0}
    clock.advance(watchers.RATING_REFRESH_SECONDS - watchers.POST_CHECK_SECONDS)
    _finish(watchers.check_due_posts(now=clock))
    assert len(synced) == 2, "an existing rating is refreshed after fifteen minutes"


def test_an_unchanged_post_check_reports_nothing_changed(client, scanned, monkeypatch):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    post_id = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    post_store.set_fields(
        post_id,
        state=lifecycle.PUBLISHED,
        remote_state="published",
        remote_post_id=4242,
        remote_published_at="2020-01-01T00:00:00Z",
    )
    image = post_store.images(post_id)[0]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE post_images SET remote_image_id=?, nsfw_level=? WHERE id=?",
            (900, 4, image["id"]),
        )

    from backend.posts import sync

    monkeypatch.setattr(
        sync,
        "sync_one",
        lambda post_id, **kw: {
            "status": "published",
            "changed": 0,
            "divergences": 0,
            "ratings_updated": 0,
        },
    )
    result = watchers.check_due_posts(now=Clock())
    _finish(result)

    assert jobs.get(result["job"]).result == {
        "changed": 0,
        "divergences": 0,
        "ratings_updated": 0,
        "released": 0,
    }


def test_a_post_check_reports_what_the_remote_sync_changed(
    client, scanned, monkeypatch
):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    post_id = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    post_store.set_fields(
        post_id,
        state=lifecycle.SCHEDULED,
        remote_state="scheduled",
        remote_post_id=4242,
        remote_published_at="2020-01-01T00:00:00Z",
    )
    image = post_store.images(post_id)[0]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE post_images SET remote_image_id=?, nsfw_level=NULL WHERE id=?",
            (900, image["id"]),
        )

    from backend.posts import sync

    def changed(synced_post_id, **kwargs):
        post_store.set_fields(
            synced_post_id,
            state=lifecycle.PUBLISHED,
            remote_state="published",
            remote_diverged=1,
        )
        with db.transaction() as conn:
            conn.execute(
                "UPDATE post_images SET nsfw_level=? WHERE id=?", (8, image["id"])
            )
        return {
            "status": "published",
            "changed": 1,
            "divergences": 1,
            "ratings_updated": 1,
        }

    monkeypatch.setattr(sync, "sync_one", changed)
    result = watchers.check_due_posts(now=Clock())
    _finish(result)

    assert jobs.get(result["job"]).result == {
        "changed": 1,
        "divergences": 1,
        "ratings_updated": 1,
        "released": 0,
    }


def test_a_post_check_reports_a_changed_remote_publish_time(
    client, scanned, monkeypatch
):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    post_id = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    post_store.set_fields(
        post_id,
        state=lifecycle.SCHEDULED,
        remote_state="scheduled",
        remote_post_id=4242,
        remote_published_at="2999-01-01T00:00:00Z",
    )
    image = post_store.images(post_id)[0]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE post_images SET remote_image_id=?, nsfw_level=? WHERE id=?",
            (900, 4, image["id"]),
        )

    from backend.posts import sync

    monkeypatch.setattr(
        sync,
        "_fetch",
        lambda remote_id: {
            "id": remote_id,
            "publishedAt": "2999-01-02T00:00:00Z",
        },
    )
    monkeypatch.setattr(sync, "_map_image_ids", lambda *args, **kwargs: 0)
    result = watchers.check_due_posts(now=Clock())
    _finish(result)

    job = jobs.get(result["job"])
    assert job.result["changed"] == 1
    fresh = post_store.get(post_id)
    assert (fresh["state"], fresh["remote_state"]) == (
        lifecycle.SCHEDULED,
        "scheduled",
    )
    assert fresh["remote_published_at"] == "2999-01-02T00:00:00Z"


def test_a_post_check_reports_a_civitai_read_failure(client, scanned, monkeypatch):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    post_id = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    post_store.set_fields(
        post_id,
        state=lifecycle.PUBLISHED,
        remote_state="published",
        remote_post_id=4242,
        remote_published_at="2020-01-01T00:00:00Z",
    )
    image = post_store.images(post_id)[0]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE post_images SET remote_image_id=?, nsfw_level=? WHERE id=?",
            (900, 4, image["id"]),
        )

    from backend.posts import sync

    def unavailable(remote_id):
        raise TransportError("connection reset")

    monkeypatch.setattr(sync, "_fetch", unavailable)
    result = watchers.check_due_posts(now=Clock())
    _finish(result)

    job = jobs.get(result["job"])
    assert job.failed == 1
    assert job.succeeded == 0
    assert job.result == {
        "changed": 0,
        "divergences": 0,
        "ratings_updated": 0,
        "released": 0,
    }
    assert job.items[-1]["status"] == "error"
    assert "connection reset" in job.items[-1]["message"]


def test_publication_settles_while_a_missing_rating_keeps_its_minute_cadence(
    client, scanned, monkeypatch
):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    clock = Clock()
    post_id = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    post_store.set_fields(
        post_id,
        state=lifecycle.SCHEDULED,
        remote_post_id=4242,
        remote_published_at="2020-01-01T00:00:00Z",
    )
    image = post_store.images(post_id)[0]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE post_images SET remote_image_id=?, nsfw_level=NULL WHERE id=?",
            (900, image["id"]),
        )

    synced = []
    from backend.posts import sync

    def publish(synced_post_id, **kwargs):
        synced.append(synced_post_id)
        post_store.set_fields(synced_post_id, state=lifecycle.PUBLISHED)

    monkeypatch.setattr(sync, "sync_one", publish)
    _finish(watchers.check_due_posts(now=clock))

    assert watchers._attempts[post_id] == watchers.MAX_POST_CHECKS
    clock.advance(watchers.POST_CHECK_SECONDS)
    _finish(watchers.check_due_posts(now=clock))
    assert synced == [post_id, post_id]


def test_a_future_scheduled_post_with_all_ratings_uses_the_slower_refresh(
    client, scanned, monkeypatch
):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    clock = Clock()
    post_id = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    post_store.set_fields(
        post_id,
        state=lifecycle.SCHEDULED,
        remote_post_id=4242,
        remote_published_at="2999-01-01T00:00:00Z",
    )
    image = post_store.images(post_id)[0]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE post_images SET remote_image_id=?, nsfw_level=? WHERE id=?",
            (900, 2, image["id"]),
        )

    synced = []
    from backend.posts import sync

    def refresh_rating(synced_post_id, **kwargs):
        synced.append(synced_post_id)
        if len(synced) == 2:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE post_images SET nsfw_level=? WHERE id=?", (8, image["id"])
                )

    monkeypatch.setattr(sync, "sync_one", refresh_rating)

    _finish(watchers.check_due_posts(now=clock))
    assert synced == [post_id]
    for _ in range(14):
        clock.advance(watchers.POST_CHECK_SECONDS)
        assert watchers.check_due_posts(now=clock) == {"checked": 0}
    clock.advance(watchers.POST_CHECK_SECONDS)
    _finish(watchers.check_due_posts(now=clock))
    assert synced == [post_id, post_id]
    assert post_store.images(post_id)[0]["nsfw_level"] == 8


def test_a_remote_draft_with_a_missing_rating_is_checked(client, scanned, monkeypatch):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    clock = Clock()
    post_id = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    post_store.set_fields(
        post_id,
        state=lifecycle.REMOTE_DRAFT,
        remote_post_id=4242,
    )
    image = post_store.images(post_id)[0]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE post_images SET remote_image_id=?, nsfw_level=NULL WHERE id=?",
            (900, image["id"]),
        )

    synced = []
    from backend.posts import sync

    monkeypatch.setattr(sync, "sync_one", lambda post_id, **kw: synced.append(post_id))
    _finish(watchers.check_due_posts(now=clock))
    clock.advance(watchers.POST_CHECK_SECONDS)
    _finish(watchers.check_due_posts(now=clock))

    assert synced == [post_id, post_id]


def test_an_archived_post_is_not_checked_for_rating_changes(client, scanned, monkeypatch):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    post_id = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    post_store.set_fields(
        post_id,
        state=lifecycle.ARCHIVED,
        remote_post_id=4242,
        remote_published_at="2020-01-01T00:00:00Z",
    )
    image = post_store.images(post_id)[0]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE post_images SET remote_image_id=?, nsfw_level=NULL WHERE id=?",
            (900, image["id"]),
        )

    synced = []
    from backend.posts import sync

    monkeypatch.setattr(sync, "sync_one", lambda post_id, **kw: synced.append(post_id))

    assert watchers.check_due_posts(now=Clock()) == {"checked": 0}
    assert synced == []


def test_a_complete_post_waits_for_the_rating_refresh_interval(
    client, scanned, monkeypatch
):
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    clock = Clock()

    post = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()
    post_id = post["id"]
    post_store.set_fields(
        post_id,
        state=lifecycle.PUBLISHED,
        remote_post_id=4242,
        remote_published_at="2020-01-01T00:00:00Z",
    )
    rows = post_store.images(post_id)
    with db.transaction() as conn:
        for row in rows:
            conn.execute(
                "UPDATE post_images SET remote_image_id=?, nsfw_level=? WHERE id=?",
                (900 + row["position"], 1, row["id"]),
            )

    synced = []
    from backend.posts import sync

    monkeypatch.setattr(sync, "sync_one", lambda post_id, **kw: synced.append(post_id))
    _finish(watchers.check_due_posts(now=clock))
    assert synced == [post_id], "checked once when its time passed"

    for _ in range(14):
        clock.advance(watchers.POST_CHECK_SECONDS)
        assert watchers.check_due_posts(now=clock) == {"checked": 0}
    clock.advance(watchers.POST_CHECK_SECONDS)
    _finish(watchers.check_due_posts(now=clock))
    assert synced == [post_id, post_id]


def test_missing_facts_names_only_what_this_application_can_decide():
    """Two facts, deliberately. A longer list would make it a general poller."""
    complete = {"state": "published"}
    rated = [{"remote_image_id": 1, "nsfw_level": 4}]
    assert watchers.missing_facts(complete, rated) == []
    future = {
        "state": "scheduled",
        "remote_published_at": "2999-01-01T00:00:00Z",
    }
    due = {
        "state": "scheduled",
        "remote_published_at": "2020-01-01T00:00:00Z",
    }
    assert watchers.missing_facts(future, rated) == []
    assert watchers.missing_facts(due, rated) == ["published"]
    assert watchers.missing_facts(complete, [{"remote_image_id": 1}]) == ["rating"]
    # An image that is not on CivitAI cannot have a rating and must not count.
    assert watchers.missing_facts(complete, [{"remote_image_id": None}]) == []


def test_a_folder_is_only_scanned_once_it_is_switched_on(client, tmp_path):
    folder = tmp_path / "watched"
    folder.mkdir()
    root = source_store.add_root(str(folder), label="Watched")

    assert watchers.check_source_roots() == {"roots": 0}, "off by default"

    source_store.set_watch_enabled(root["id"], True)
    _finish(watchers.check_source_roots())


def test_a_model_folder_is_only_hashed_once_it_is_switched_on(client, tmp_path):
    folder = tmp_path / "models"
    folder.mkdir()
    root = model_store.add_root(str(folder), label="Models")

    assert watchers.check_model_roots() == {"roots": 0}, "off by default"

    model_store.set_watch_enabled(root["id"], True)
    _finish(watchers.check_model_roots())


def test_a_watched_folder_never_destroys_anything(client, scanned, tmp_path, fixture_images):
    """`PRI-03` and `PRI-04`, measured rather than grepped.

    A grep over the module missed this entirely: the check reaches
    `mark_missing` through the scan it starts, and that deletes `images` rows
    with `image_edits` cascading off them. Moving a file between folders would
    have destroyed the maintainer's edits within fifteen minutes, unattended.
    """
    from backend import db

    folder, images = fixture_images
    root = source_store.add_root(str(folder), label="Watched")
    source_store.set_watch_enabled(root["id"], True)

    image_id = scanned[0]
    client.put(
        f"/api/images/{image_id}/edit",
        json={"draft": {"fields": {"Steps": 17}}, "touched": ["Steps"], "deleted": []},
    )
    edits_before = db.get_connection().execute(
        "SELECT COUNT(*) FROM image_edits"
    ).fetchone()[0]
    assert edits_before == 1

    Path(db.get_connection().execute(
        "SELECT absolute_path FROM images WHERE id=?", (image_id,)
    ).fetchone()["absolute_path"]).unlink()

    _finish(watchers.check_source_roots())

    assert db.get_connection().execute(
        "SELECT COUNT(*) FROM image_edits"
    ).fetchone()[0] == edits_before, "an unattended pass destroyed a user edit"
    assert db.get_connection().execute(
        "SELECT COUNT(*) FROM images WHERE id=?", (image_id,)
    ).fetchone()[0] == 1, "and the row it hung off"


def test_a_watcher_leaves_a_divergence_standing(client, scanned, monkeypatch):
    """`LIF-11`: surface it, never pick a side - checked on the rows, not by grep."""
    from backend import db

    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    clock = Clock()
    post_id = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    post_store.set_fields(
        post_id,
        state=lifecycle.PUBLISHED,
        remote_post_id=4242,
        remote_published_at="2020-01-01T00:00:00Z",
        remote_diverged=1,
        pushed_fields_json='{"title": "what CivitAI held"}',
    )

    from backend.posts import sync

    monkeypatch.setattr(sync, "sync_one", lambda post_id, **kw: None)
    _finish(watchers.check_due_posts(now=clock))

    fresh = post_store.get(post_id)
    assert fresh["remote_diverged"] is True, "the banner survived the check"
    assert fresh["pushed_fields"] == {"title": "what CivitAI held"}, "no side was picked"


def test_the_post_check_never_overwrites_local_intent(client, scanned, monkeypatch):
    """`LIF-11` again: `sync_one`'s default writes CivitAI's hideMeta over ours."""
    db.set_setting(watchers.SETTING_WATCH_POSTS, "1")
    clock = Clock()
    post_id = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    post_store.set_fields(
        post_id,
        state=lifecycle.PUBLISHED,
        remote_post_id=4242,
        remote_published_at="2020-01-01T00:00:00Z",
    )

    seen = {}
    from backend.posts import sync

    monkeypatch.setattr(
        sync, "sync_one", lambda post_id, **kw: seen.update(kw)
    )
    _finish(watchers.check_due_posts(now=clock))

    assert seen.get("preserve_hide_meta") is True


def test_a_watched_folder_moves_no_file(client, scanned, tmp_path):
    """`ARC-04`: no automatism moves files - the archive folder stays empty."""
    archive = tmp_path / "archiv"
    archive.mkdir()
    source_store.add_root(str(archive), label="Archiv", is_archive=True)
    root = source_store.list_roots()[0]
    source_store.set_watch_enabled(root["id"], True)

    _finish(watchers.check_source_roots())

    assert list(archive.iterdir()) == [], "an unattended pass filed something away"


def _finish(result: dict) -> dict:
    """A check starts a real job on a daemon thread; wait for that job.

    Waiting on "nothing is running" would be a race: the thread may not have
    been scheduled yet when the test looks.
    """
    import time

    job_id = result.get("job")
    assert job_id is not None, f"the check started no job: {result}"
    for _ in range(500):
        job = jobs.get(job_id)
        if job is not None and job.status not in ("starting", "running"):
            assert job.status != "error", job.error
            return result
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")
