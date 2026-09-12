"""Keeping a remote post's image order aligned with its local sequence."""

from __future__ import annotations

from typing import Any

from backend import db
from backend.civitai import trpc
from backend.civitai.errors import CivitaiError
from backend.store import posts as post_store


def _post_with_unmatched_image(client, image_id: int) -> tuple[int, int, int]:
    post_id = client.post("/api/posts", json={"image_ids": [image_id]}).json()["id"]
    matched_id = int(post_store.images(post_id)[0]["id"])
    with db.transaction() as conn:
        cursor = conn.execute(
            """
            INSERT INTO post_images(
                post_id, image_id, position, source_path, sha256, remote_image_id, remote_url
            ) VALUES(?,NULL,?,?,?,?,?)
            """,
            (post_id, 1, "/remote/unmatched.png", "remote-sha", 99001, "https://remote/image"),
        )
    return post_id, matched_id, int(cursor.lastrowid)


def _scheduled_remote_post(client, scanned: list[int]) -> tuple[int, dict[int, int]]:
    post_id = client.post("/api/posts", json={"image_ids": scanned}).json()["id"]
    post_store.set_fields(
        post_id,
        state="scheduled",
        remote_post_id=4242,
        remote_state="scheduled",
        dirty=0,
    )
    mapping: dict[int, int] = {}
    with db.transaction() as conn:
        for index, image in enumerate(post_store.images(post_id)):
            remote_image_id = 9000 + index
            mapping[int(image["image_id"])] = remote_image_id
            conn.execute(
                "UPDATE post_images SET remote_image_id=? WHERE id=?",
                (remote_image_id, image["id"]),
            )
    return post_id, mapping


def test_reordering_a_remote_post_sends_the_remote_ids_before_saving_locally(
    client, scanned, monkeypatch
):
    post_id, mapping = _scheduled_remote_post(client, scanned)
    wanted = list(reversed(scanned))
    sent: list[tuple[str, dict[str, Any]]] = []

    def record(procedure: str, payload: dict[str, Any], **_kwargs: Any) -> list[Any]:
        assert db.get_connection().in_transaction is False
        assert [image["image_id"] for image in post_store.images(post_id)] == scanned
        sent.append((procedure, payload))
        return []

    monkeypatch.setattr(trpc, "mutate", record)

    response = client.put(f"/api/posts/{post_id}/images", json={"image_ids": wanted})

    assert response.status_code == 200
    assert sent == [
        (
            "post.reorderImages",
            {"id": 4242, "imageIds": [mapping[image_id] for image_id in wanted]},
        )
    ]
    assert [image["image_id"] for image in post_store.images(post_id)] == wanted


def test_a_remote_reorder_failure_keeps_the_local_order(client, scanned, monkeypatch):
    post_id, _mapping = _scheduled_remote_post(client, scanned)
    before = [image["image_id"] for image in post_store.images(post_id)]

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise CivitaiError("remote refusal", status=409)

    monkeypatch.setattr(trpc, "mutate", refuse)

    response = client.put(
        f"/api/posts/{post_id}/images", json={"image_ids": list(reversed(scanned))}
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "post_image_reorder_failed",
        "message": "CivitAI did not accept the new image order. Retry the reorder or "
        "sync the post before trying again.",
        "params": {},
    }
    assert [image["image_id"] for image in post_store.images(post_id)] == before
    assert post_store.get(post_id)["dirty"] is False


def test_an_incomplete_remote_id_list_stays_local_for_the_next_push(
    client, scanned, monkeypatch
):
    post_id, _mapping = _scheduled_remote_post(client, scanned)
    wanted = list(reversed(scanned))
    missing_remote_id = wanted[0]
    with db.transaction() as conn:
        conn.execute(
            "UPDATE post_images SET remote_image_id=NULL WHERE post_id=? AND image_id=?",
            (post_id, missing_remote_id),
        )
    sent: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(
        trpc,
        "mutate",
        lambda procedure, payload, **_kwargs: sent.append((procedure, payload)),
    )

    response = client.put(f"/api/posts/{post_id}/images", json={"image_ids": wanted})

    assert response.status_code == 200
    assert sent == []
    assert [image["image_id"] for image in post_store.images(post_id)] == wanted
    assert post_store.get(post_id)["dirty"] is True


def test_image_order_reorders_matched_and_unmatched_tiles(client, scanned):
    post_id, matched_id, unmatched_id = _post_with_unmatched_image(client, scanned[0])

    response = client.put(
        f"/api/posts/{post_id}/image-order",
        json={"post_image_ids": [unmatched_id, matched_id]},
    )

    assert response.status_code == 200
    assert [image["id"] for image in response.json()["images"]] == [unmatched_id, matched_id]
    assert [image["image_id"] for image in response.json()["images"]] == [None, scanned[0]]


def test_image_order_removes_a_matched_tile_without_removing_an_unmatched_one(client, scanned):
    post_id, _matched_id, unmatched_id = _post_with_unmatched_image(client, scanned[0])

    response = client.put(
        f"/api/posts/{post_id}/image-order", json={"post_image_ids": [unmatched_id]}
    )

    assert response.status_code == 200
    assert [image["id"] for image in response.json()["images"]] == [unmatched_id]
    assert response.json()["images"][0]["image_id"] is None


def test_library_image_route_adds_without_dropping_an_unmatched_tile(client, scanned):
    post_id, _matched_id, unmatched_id = _post_with_unmatched_image(client, scanned[0])

    response = client.put(
        f"/api/posts/{post_id}/images", json={"image_ids": scanned[:2]}
    )

    assert response.status_code == 200
    assert [image["image_id"] for image in response.json()["images"]] == [
        scanned[0],
        None,
        scanned[1],
    ]
    unmatched = next(image for image in response.json()["images"] if image["image_id"] is None)
    assert unmatched["source_path"] == "/remote/unmatched.png"


def test_image_order_refuses_more_than_the_post_limit(client):
    post_id = client.post("/api/posts", json={}).json()["id"]

    response = client.put(
        f"/api/posts/{post_id}/image-order", json={"post_image_ids": list(range(1, 22))}
    )

    assert response.status_code == 400
    assert response.json()["detail"] == {
        "code": "post_image_limit",
        "message": "A post can have at most 20 images; 21 were selected. Remove 1 image(s).",
        "params": {"limit": 20, "count": 21, "excess": 1},
    }
