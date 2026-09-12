"""Discovery of CivitAI posts that are not represented locally."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from backend import db, jobs
from backend.civitai import trpc
from backend.civitai.errors import TransportError
from backend.posts import lifecycle, sync
from backend.store import posts as post_store


def test_post_listing_serialises_explicit_sort_and_opaque_cursor(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    def query(procedure: str, payload: dict[str, Any]):
        calls.append((procedure, payload))
        return {"items": [], "nextCursor": "next"}

    monkeypatch.setattr(trpc, "query", query)

    trpc.post_get_infinite(
        username="tester",
        scheduled=True,
        limit=200,
        sort="Newest",
        cursor="opaque:cursor",
    )

    assert calls == [
        (
            "post.getInfinite",
            {
                "username": "tester",
                "scheduled": True,
                "limit": 200,
                "sort": "Newest",
                "cursor": "opaque:cursor",
            },
        )
    ]


def test_global_sync_skips_a_pushing_post_and_syncs_the_normal_post_beside_it(
    client, monkeypatch
):
    pushing = post_store.create(title="Owned by a push")
    normal = post_store.create(title="Normal remote post")
    post_store.set_fields(pushing, state=lifecycle.PUSHING, remote_post_id=701)
    post_store.set_fields(normal, remote_post_id=702)
    calls: list[int] = []

    def sync_one(post_id: int) -> dict[str, Any]:
        calls.append(post_id)
        post_store.set_fields(post_id, remote_synced_at="2026-09-12T12:00:00Z")
        return {"post_id": post_id, "status": "draft", "diverged": False}

    monkeypatch.setattr(sync, "sync_one", sync_one)

    started = client.post("/api/sync").json()
    deadline = time.monotonic() + 2
    while (job := jobs.get(started["id"])) is not None and job.status in (
        "starting",
        "running",
    ):
        assert time.monotonic() < deadline, "the sync job never finished"
        time.sleep(0.005)

    assert job is not None and job.status == "done", job.error if job else None
    assert calls == [normal]
    assert post_store.get(pushing)["remote_synced_at"] is None
    assert post_store.get(pushing)["state"] == lifecycle.PUSHING
    assert post_store.get(normal)["remote_synced_at"] == "2026-09-12T12:00:00Z"


def test_discover_requests_newest_page_and_returns_typed_cards(client, monkeypatch):
    known_post_id = post_store.create(title="Known")
    post_store.set_fields(known_post_id, remote_post_id=101)
    calls: list[dict[str, Any]] = []

    def listing(**kwargs):
        calls.append(kwargs)
        return {
            "nextCursor": "opaque:page-2",
            "items": [
                {"id": 104, "title": None, "publishedAt": None, "images": [{}]},
                {"id": 101, "title": "Known", "publishedAt": "2026-08-01T10:00:00Z"},
                {
                    "id": 102,
                    "title": "Missing",
                    "publishedAt": "2026-08-22T10:00:00Z",
                    "imageCount": 7,
                    "images": [{"url": "cover-key"}, {"url": "second-key"}],
                },
                {
                    "id": 103,
                    "title": "Scheduled",
                    "publishedAt": "2026-09-02T10:00:00Z",
                    "imageCount": 2,
                    "images": [],
                },
            ]
        }

    monkeypatch.setattr(sync.account, "username", lambda: "tester")
    monkeypatch.setattr(sync.trpc, "post_get_infinite", listing)
    monkeypatch.setattr(
        sync.schedule, "utcnow", lambda: datetime(2026, 8, 23, tzinfo=timezone.utc)
    )

    response = client.get(
        "/api/sync/discover", params={"count": 37, "cursor": "opaque:page-1"}
    )

    assert response.status_code == 200
    body = response.json()
    assert [(item["remote_post_id"], item["state"]) for item in body["items"]] == [
        (103, "scheduled"),
        (102, "published"),
        (104, "draft"),
    ]
    assert body["items"][1]["image_count"] == 7
    assert body["items"][1]["cover_url"].endswith("/cover-key/width=450")
    assert body["items"][2]["cover_url"] is None
    assert body["summary"] == {
        "examined": 4,
        "outside_period": 0,
        "already_known": 1,
        "adoptable": 3,
    }
    assert body["next_cursor"] == "opaque:page-2"
    assert calls == [
        {
            "username": "tester",
            "scheduled": True,
            "limit": 37,
            "sort": "Newest",
            "cursor": "opaque:page-1",
        }
    ]


def test_discover_period_counts_hidden_rows_and_stops_before_older_page(client, monkeypatch):
    calls: list[dict[str, Any]] = []
    known_post_id = post_store.create(title="Known")
    post_store.set_fields(known_post_id, remote_post_id=203)

    def listing(**kwargs):
        calls.append(kwargs)
        return {
            "nextCursor": "older-page",
            "items": [
                {"id": 201, "title": "Old", "publishedAt": "2026-06-01T10:00:00Z"},
                {
                    "id": 202,
                    "title": "Recent",
                    "publishedAt": "2026-08-10T10:00:00Z",
                },
                {
                    "id": 203,
                    "title": "Known",
                    "publishedAt": "2026-08-15T10:00:00Z",
                },
            ]
        }

    monkeypatch.setattr(sync.account, "username", lambda: "tester")
    monkeypatch.setattr(sync.trpc, "post_get_infinite", listing)
    monkeypatch.setattr(
        sync.schedule, "utcnow", lambda: datetime(2026, 8, 23, tzinfo=timezone.utc)
    )

    response = client.get("/api/sync/discover", params={"count": 23, "days": 30})

    assert response.status_code == 200
    body = response.json()
    assert [item["remote_post_id"] for item in body["items"]] == [202]
    assert body["summary"] == {
        "examined": 3,
        "outside_period": 1,
        "already_known": 1,
        "adoptable": 1,
    }
    assert body["range_complete"] is True
    assert body["next_cursor"] is None
    assert calls == [
        {
            "username": "tester",
            "scheduled": True,
            "limit": 23,
            "sort": "Newest",
            "cursor": None,
        }
    ]


def test_batch_adoption_skips_known_and_continues_after_one_failure(monkeypatch):
    calls: list[int] = []

    def adopt(remote_post_id: int) -> dict[str, Any]:
        assert not db.get_connection().in_transaction
        calls.append(remote_post_id)
        if remote_post_id == 302:
            return {"post_id": 12, "created": False}
        if remote_post_id == 303:
            raise ValueError("Post 303 belongs to someone else.")
        return {"post_id": remote_post_id + 1000, "created": True}

    monkeypatch.setattr(sync, "import_remote", adopt)
    job = jobs.Job(id=1, kind="adopt")

    result = sync.import_remote_batch(job, [301, 302, 303, 304])

    assert calls == [301, 302, 303, 304]
    assert (job.processed, job.succeeded, job.skipped, job.failed) == (4, 2, 1, 1)
    assert [item["outcome"] for item in result["outcomes"]] == [
        "created",
        "skipped",
        "failed",
        "created",
    ]
    assert result["outcomes"][1]["post_id"] == 12
    assert "belongs to" in result["outcomes"][2]["message"]


def test_batch_adoption_names_each_refusal_and_counts_local_images(
    monkeypatch, client, scanned
):
    """One line per id, and it says what happened - the maintainer reads this
    list to decide which posts still need their images fetched."""
    from backend.civitai.errors import NotFound
    from backend.store import posts as post_store

    adopted = post_store.create(title="Adopted")
    post_store.set_fields(adopted, remote_post_id=601)
    post_store.set_images(adopted, scanned[:1])

    def adopt(remote_post_id: int):
        if remote_post_id == 601:
            return {"post_id": adopted, "created": True}
        if remote_post_id == 602:
            return {"post_id": adopted, "created": False}
        if remote_post_id == 603:
            raise sync.NotYours("Post 603 belongs to somebody, not to you.")
        if remote_post_id == 604:
            raise NotFound("post.get: 604 not found")
        raise RuntimeError("connection dropped")

    monkeypatch.setattr(sync, "import_remote", adopt)
    job = jobs.Job(id=1, kind="adopt")

    result = sync.import_remote_batch(job, [601, 602, 603, 604, 605])

    by_id = {item["remote_post_id"]: item for item in result["outcomes"]}
    assert [by_id[i]["outcome"] for i in (601, 602, 603, 604, 605)] == [
        "created",
        "skipped",
        "not_yours",
        "not_found",
        "failed",
    ]
    assert (by_id[601]["images"], by_id[601]["images_local"]) == (1, 1)
    assert "belongs to" in by_id[603]["message"]
    assert by_id[604]["images"] == 0


def test_batch_adoption_reports_a_post_whose_images_are_not_local(
    monkeypatch, client, scanned
):
    """"none found locally" has to be distinguishable from "all found"."""
    from backend import db
    from backend.store import posts as post_store

    adopted = post_store.create(title="Remote only")
    post_store.set_fields(adopted, remote_post_id=701)
    with db.transaction() as conn:
        for position in range(3):
            conn.execute(
                "INSERT INTO post_images(post_id, image_id, position, source_path, sha256,"
                " content_type, media_type, remote_image_id)"
                " VALUES(?,NULL,?,'','','image/png','image',?)",
                (adopted, position, 900 + position),
            )

    monkeypatch.setattr(
        sync, "import_remote", lambda remote_post_id: {"post_id": adopted, "created": True}
    )
    job = jobs.Job(id=1, kind="adopt")

    result = sync.import_remote_batch(job, [701])

    outcome = result["outcomes"][0]
    assert (outcome["images"], outcome["images_local"]) == (3, 0)


def test_batch_adoption_observes_cancellation_between_posts(monkeypatch):
    job = jobs.Job(id=2, kind="adopt")
    calls: list[int] = []

    def adopt(remote_post_id: int) -> dict[str, Any]:
        calls.append(remote_post_id)
        job.cancel()
        return {"post_id": 22, "created": True}

    monkeypatch.setattr(sync, "import_remote", adopt)

    sync.import_remote_batch(job, [401, 402])

    assert calls == [401]
    assert job.processed == 1
    assert job.result["outcomes"] == [
        {
            "remote_post_id": 401,
            "post_id": 22,
            "outcome": "created",
            "images": 0,
            "images_local": 0,
            "message": "",
        }
    ]


def test_owner_detail_refreshes_image_facts_and_reorder_preserves_origin(
    client, scanned, monkeypatch
):
    post_id = post_store.create(title="Owner detail")
    post_store.set_images(post_id, scanned[:2])
    rows = post_store.images(post_id)
    with db.transaction() as conn:
        for index, row in enumerate(rows, start=1):
            conn.execute(
                "UPDATE post_images SET remote_uuid=?, remote_image_id=?, remote_url=?,"
                " hide_meta=0, remote_on_site=NULL WHERE id=?",
                (f"uuid-{index}", 700 + index, f"https://old/{index}", row["id"]),
            )

    detail = {
        "images": [
            {
                "id": 701,
                "url": "uuid-1",
                "hideMeta": True,
                "meta": {"civitaiResources": [], "Model": "urn:air:sdxl:model:1"},
            },
            {
                "id": 702,
                "url": "uuid-2",
                "hideMeta": False,
                "meta": {"civitaiResources": [], "Model": "external.safetensors"},
            },
            {
                "id": 703,
                "url": "remote-only",
                "hideMeta": True,
                "meta": {"civitaiResources": []},
                "width": 800,
                "height": 1200,
                "mimeType": "image/png",
                "type": "image",
            },
        ]
    }
    monkeypatch.setattr(sync.trpc, "post_get_edit", lambda remote_id: detail)

    sync._map_image_ids(post_id, 900)

    refreshed = post_store.images(post_id)
    assert len(refreshed) == 3
    facts = [
        (row["remote_image_id"], row["hide_meta"], row["remote_on_site"])
        for row in refreshed
    ]
    assert facts == [
        (701, True, True),
        (702, False, False),
        (703, True, True),
    ]
    assert refreshed[2]["image_id"] is None

    # The id stays the same while owner-controlled facts and the upload key
    # change. Matching by the already-known numeric id must still refresh them.
    detail["images"][0] = {
        "id": 701,
        "url": "uuid-1-refreshed",
        "hideMeta": False,
        "meta": {"civitaiResources": [], "Version": "external"},
    }
    sync._map_image_ids(post_id, 900)
    again = post_store.images(post_id)
    assert len(again) == 3
    first = next(row for row in again if row["remote_image_id"] == 701)
    assert first["hide_meta"] is False
    assert first["remote_on_site"] is False
    assert "uuid-1-refreshed" in first["remote_url"]

    post_store.set_images(post_id, list(reversed(scanned[:2])))
    reordered = post_store.images(post_id)
    assert {row["remote_image_id"]: row["remote_on_site"] for row in reordered} == {
        701: False,
        702: False,
        703: True,
    }

    api_images = client.get(f"/api/posts/{post_id}").json()["images"]
    assert all(isinstance(row["remote_on_site"], bool) for row in api_images)


def test_failed_owner_detail_leaves_unknown_origin_unknown(client, scanned, monkeypatch):
    post_id = post_store.create(title="Unknown owner detail")
    post_store.set_images(post_id, scanned[:1])
    row = post_store.images(post_id)[0]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE post_images SET remote_uuid='unknown', remote_on_site=NULL WHERE id=?",
            (row["id"],),
        )

    def unavailable(remote_id: int):
        raise TransportError("owner detail unavailable")

    monkeypatch.setattr(sync.trpc, "post_get_edit", unavailable)
    sync._map_image_ids(post_id, 900)

    stored = post_store.images(post_id)[0]
    assert stored["remote_on_site"] is None
    assert client.get(f"/api/posts/{post_id}").json()["images"][0]["remote_on_site"] is None


def test_a_sync_keeps_civitais_rating_and_a_reorder_does_not_lose_it(
    client, scanned, monkeypatch
):
    """The rating is read on every sync and must survive `set_images`.

    `set_images` deletes and re-inserts every row from a hand-written column
    list, so a per-image column missing from it is silently dropped on the next
    reorder - the trap `.claude/rules/push-pipeline.md` warns about.
    """
    post_id = post_store.create(title="Rated")
    post_store.set_images(post_id, scanned[:2])
    rows = post_store.images(post_id)
    with db.transaction() as conn:
        for index, row in enumerate(rows, start=1):
            conn.execute(
                "UPDATE post_images SET remote_uuid=?, remote_image_id=?, remote_url=?"
                " WHERE id=?",
                (f"uuid-{index}", 800 + index, f"https://old/{index}", row["id"]),
            )

    detail = {
        "images": [
            {"id": 801, "url": "uuid-1", "hideMeta": False, "meta": {}, "nsfwLevel": 4},
            # No rating yet: CivitAI has not looked at this one.
            {"id": 802, "url": "uuid-2", "hideMeta": False, "meta": {}, "nsfwLevel": 0},
        ]
    }
    monkeypatch.setattr(sync.trpc, "post_get_edit", lambda remote_id: detail)
    assert sync._map_image_ids(post_id, 900) == 1

    stored = post_store.images(post_id)
    assert [row["nsfw_level"] for row in stored] == [4, None]
    assert [row["nsfw_label"] for row in stored] == ["R", None]
    assert stored[1]["nsfw_label"] != "PG", "0 is unrated, never PG"
    assert sync._map_image_ids(post_id, 900) == 0

    # The reorder that used to drop it.
    post_store.set_images(post_id, list(reversed(scanned[:2])))
    after = post_store.images(post_id)
    assert {row["image_id"]: row["nsfw_label"] for row in after} == {
        scanned[0]: "R",
        scanned[1]: None,
    }


def test_every_rating_bit_maps_to_civitais_own_word():
    """Transcribed from the checkout, not invented."""
    from backend import config

    assert config.NSFW_LEVEL_LABELS == {
        1: "PG",
        2: "PG-13",
        4: "R",
        8: "X",
        16: "XXX",
        32: "Blocked",
    }
    assert 0 not in config.NSFW_LEVEL_LABELS, "0 is unrated, and has no label here"
