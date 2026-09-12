"""The push pipeline against a fake CivitAI.

The failure this suite exists for: ``create_post`` has no idempotency key, so a
lost response leaves the app unable to tell whether the post exists. A naive
retry creates a second one. Every "interrupted after step N" test below checks
that resuming does the right thing - above all, that it never creates twice.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from backend import config, db, jobs
from backend.civitai import client as civitai_client
from backend.civitai import trpc as civitai_trpc
from backend.civitai.errors import CivitaiError, NotFound, TransportError
from backend.posts import lifecycle, push, schedule
from backend.store import edits
from backend.store import posts as post_store
from backend.store import runs as run_store

_REAL_POST_CREATE = civitai_trpc.post_create


class FakeCivitai:
    """Records every call and can be told to fail at a chosen point."""

    def __init__(self):
        self.uploads: list[str] = []
        self.uploaded_bytes: list[bytes] = []
        self.created: list[dict[str, Any]] = []
        self.updates: list[dict[str, Any]] = []
        self.image_updates: list[dict[str, Any]] = []
        self.tags_added: list[tuple[int, str]] = []
        self.tags_removed: list[tuple[int, int]] = []
        self.deleted: list[int] = []
        self.images_added: list[tuple[int, str]] = []
        self.calls: list[str] = []
        self.fail_at: str | None = None
        self.failure_detail: str | None = None
        self.ignore_empty_detail = False
        self._next_post_id = 5000
        self._next_uuid = 0
        self._next_tag_id = 100
        self.http_outcomes: list[httpx.Response | httpx.HTTPError] = []
        self.http_calls = 0
        #: Which route created each post, so a test can assert the preferred one.
        self.created_via: list[str] = []
        #: Server-side state, so the fake answers reads with what writes did.
        self.posts: dict[int, dict[str, Any]] = {}

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def close(self):
        """The shared pool is closed between tests; this stands in for it."""

    def post(self, url, *, json, headers):
        """Return or raise the supplied HTTP outcomes, one POST at a time."""
        self.http_calls += 1
        outcome = self.http_outcomes.pop(0)
        if isinstance(outcome, httpx.HTTPError):
            raise outcome
        return outcome

    def upload_image(self, path, content_type=None):
        if self.fail_at == "upload":
            raise CivitaiError("upload failed")
        self.calls.append("upload_image")
        self._next_uuid += 1
        uuid = f"uuid-{self._next_uuid:04d}"
        self.uploads.append(str(path))
        self.uploaded_bytes.append(path.read_bytes())
        return {"uuid": uuid, "width": 832, "height": 1216, "content_type": "image/png"}

    def post_create(self, **kwargs):
        if self.fail_at == "create":
            raise CivitaiError("connection dropped", detail=self.failure_detail)
        self.calls.append("post.create")
        result = self._make_post(kwargs)
        self.created_via.append("trpc")
        return result

    def _make_post(self, kwargs):
        self._next_post_id += 1
        post_id = self._next_post_id
        self.created.append(kwargs)
        raw_tags = kwargs.get("tags") or []
        if isinstance(raw_tags, str):
            raw_tags = [part.strip() for part in raw_tags.split(",") if part.strip()]
        tags = {}
        for name in raw_tags:
            self._next_tag_id += 1
            tags[name.lower()] = self._next_tag_id
        self.posts[post_id] = {
            "id": post_id,
            "title": kwargs.get("title") or "",
            "detail": kwargs.get("detail") or "",
            "publishedAt": kwargs.get("published_at"),
            "tags": tags,
            # post.create takes no images at all (LIF-18); post.addImage fills this.
            "images": [],
        }
        return {"id": post_id, "publishedAt": kwargs.get("published_at")}

    def post_add_image(self, post_id, image):
        if self.fail_at == "add_image":
            raise CivitaiError("post.addImage failed")
        self.calls.append("post.addImage")
        state = self.posts[post_id]
        url = image["url"]
        existing = next((entry for entry in state["images"] if entry.get("url") == url), None)
        if existing is None:
            entry = {**image, "id": 900000 + len(state["images"]), "url": url}
            state["images"].append(entry)
            self.images_added.append((post_id, url))
        else:
            entry = existing
        if self.fail_at == "add_image_response":
            raise CivitaiError("post.addImage response lost")
        return entry

    def get_post(self, post_id):
        """Mirrors the real MCP tool, which returns NO tags - that omission is
        what made the marker-tag bug possible, so the fake reproduces it."""
        state = self.posts.get(post_id)
        if state is None:
            raise NotFound(f"get_post: {post_id} not found")
        return {
            "id": post_id,
            "title": state.get("title", ""),
            "publishedAt": state.get("publishedAt"),
            "published": bool(state.get("publishedAt")),
        }

    def query(self, procedure, payload=None):
        """Only post.getEdit is asked for through the generic query helper."""
        if procedure != "post.getEdit":
            raise CivitaiError(f"unerwartet: {procedure}")
        state = self.posts.get((payload or {}).get("id"))
        if state is None:
            raise NotFound("post.getEdit: not found")
        return {
            "id": state["id"],
            "images": [
                {
                    "id": image.get("id", 900000 + index),
                    "url": image.get("url") or image.get("uuid"),
                    "hideMeta": image.get("hideMeta", False),
                }
                for index, image in enumerate(state.get("images") or [])
            ],
        }

    def post_get(self, post_id):
        """The tRPC read, which does carry the tags."""
        self.calls.append("post.get")
        state = self.posts.get(post_id)
        if state is None:
            raise CivitaiError("post.get: not found")
        if self.fail_at == "post_get":
            raise CivitaiError("post.get failed")
        if self.fail_at == "post_get_shape":
            return []
        return {
            "id": post_id,
            "title": state["title"],
            "detail": state["detail"],
            "publishedAt": state["publishedAt"],
            "tags": [{"id": tag_id, "name": name} for name, tag_id in state["tags"].items()],
        }

    def delete_post(self, post_id):
        self.deleted.append(post_id)

    def publish_post(self, post_id):
        self.calls.append("publish_post")
        return {"id": post_id}

    def post_update(self, post_id, **kwargs):
        if self.fail_at == "update":
            raise CivitaiError("post.update failed")
        self.calls.append("post.update")
        self.updates.append({"id": post_id, **kwargs})
        if post_id in self.posts:
            if kwargs.get("published_at"):
                self.posts[post_id]["publishedAt"] = kwargs["published_at"]
            if "title" in kwargs:
                self.posts[post_id]["title"] = kwargs["title"]
            if "detail" in kwargs and not (
                self.ignore_empty_detail and kwargs["detail"] == ""
            ):
                self.posts[post_id]["detail"] = kwargs["detail"]
        if self.fail_at == "update_response":
            raise CivitaiError("post.update response lost")
        if kwargs.get("published_at"):
            return self.post_get(post_id)
        return None

    def post_update_image(self, image_id, *, hide_meta):
        self.calls.append("post.updateImage")
        self.image_updates.append({"id": image_id, "hide_meta": hide_meta})
        if self.fail_at == "update_image":
            raise CivitaiError("post.updateImage failed")
        for state in self.posts.values():
            image = next((item for item in state["images"] if item.get("id") == image_id), None)
            if image is not None:
                image["hideMeta"] = hide_meta
                return image
        raise CivitaiError("post.updateImage: not found")

    def post_add_tag(self, post_id, name):
        if self.fail_at == "tag":
            raise CivitaiError("tag call failed")
        self.calls.append("post.addTag")
        self._next_tag_id += 1
        self.tags_added.append((post_id, name))
        if post_id in self.posts:
            self.posts[post_id]["tags"][name.lower()] = self._next_tag_id
        return {"id": self._next_tag_id, "name": name}

    def post_remove_tag(self, post_id, tag_id):
        if self.fail_at == "untag":
            raise CivitaiError("the marker could not be removed")
        self.calls.append("post.removeTag")
        self.tags_removed.append((post_id, tag_id))
        state = self.posts.get(post_id)
        if state:
            for name, existing in list(state["tags"].items()):
                if existing == tag_id:
                    del state["tags"][name]
        return {}


@pytest.fixture
def fake(monkeypatch):
    stub = FakeCivitai()
    import backend.posts.sync as sync_module

    monkeypatch.setattr(push.mcp, "upload_image", stub.upload_image)
    monkeypatch.setattr(push.mcp, "get_post", stub.get_post)
    monkeypatch.setattr(push.mcp, "publish_post", stub.publish_post)
    monkeypatch.setattr(push.trpc, "post_update", stub.post_update)
    monkeypatch.setattr(push.trpc, "post_update_image", stub.post_update_image)
    monkeypatch.setattr(push.trpc, "post_create", stub.post_create)
    monkeypatch.setattr(push.trpc, "post_add_image", stub.post_add_image)
    monkeypatch.setattr(push.trpc, "post_add_tag", stub.post_add_tag)
    monkeypatch.setattr(push.trpc, "post_remove_tag", stub.post_remove_tag)
    monkeypatch.setattr(push.trpc, "post_get", stub.post_get)
    monkeypatch.setattr(sync_module.mcp, "get_post", stub.get_post)
    monkeypatch.setattr(sync_module.trpc, "post_get", stub.post_get)
    monkeypatch.setattr(sync_module.trpc, "query", stub.query)
    import backend.posts.reconcile as reconcile_module

    monkeypatch.setattr(reconcile_module.mcp, "get_post", stub.get_post)
    monkeypatch.setattr(push.ratelimit, "check", lambda pending=1: None)
    monkeypatch.setattr(
        push.account, "ensure_can_write", lambda account=None: {"username": "tester"}
    )
    monkeypatch.setattr(push.account, "refresh", lambda: {"username": "tester"})
    return stub


@pytest.fixture
def ready_post(client, scanned):
    post = client.post(
        "/api/posts",
        json={
            "title": "Testpost",
            "image_ids": scanned,
            "tags": ["alpha", "beta"],
            "schedule_mode": "relative",
            "schedule_offset_minutes": 120,
        },
    ).json()
    return post["id"]


def run_push(post_id: int) -> jobs.Job:
    job = jobs.Job(id=0, kind="push")
    push.push_posts(job, [post_id])
    return job


def action_events() -> list[dict[str, Any]]:
    found = []
    for path in config.data_dir().glob(f"{config.DIAGNOSTIC_LOG_FILENAME}*"):
        for line in path.read_text(encoding="utf-8").splitlines():
            _, marker, payload = line.partition(" INFO ")
            if marker:
                event = json.loads(payload)
                if event.get("event") == "action":
                    found.append(event)
    return found


# --- the happy path ---------------------------------------------------------


def test_a_full_push_creates_a_draft_attaches_images_then_sets_the_time(
    fake, ready_post, scanned
):
    run_push(ready_post)

    assert len(fake.uploads) == len(scanned)
    assert len(fake.created) == 1
    assert fake.created_via == ["trpc"]
    assert "published_at" not in fake.created[0]
    assert "images" not in fake.created[0], "post.create is deliberately empty"
    remote_id = post_store.get(ready_post)["remote_post_id"]
    first_image = fake.posts[remote_id]["images"][0]
    assert first_image["metadata"] == {
        "hash": first_image["hash"],
        "size": len(fake.uploaded_bytes[0]),
        "width": first_image["width"],
        "height": first_image["height"],
    }
    assert first_image["meta"]["prompt"]
    assert isinstance(first_image["meta"]["Model"], str)
    assert "hashes" not in first_image["meta"]
    assert all(image.get("meta") for image in fake.posts[remote_id]["images"])
    assert len(fake.updates) == 1
    assert fake.updates[0]["published_at"].endswith("Z")
    assert fake.calls.index("post.create") < fake.calls.index("upload_image")
    assert fake.calls.index("upload_image") < fake.calls.index("post.addImage")
    assert fake.calls.index("post.addImage") < fake.calls.index("post.update")
    assert run_store.STEP_ORDER.index(run_store.ATTACHED) < run_store.STEP_ORDER.index(
        run_store.PUBLISH_APPLIED
    )

    post = post_store.get(ready_post)
    assert post["remote_post_id"] is not None
    assert post["state"] == lifecycle.SCHEDULED


def test_hide_meta_set_before_the_first_push_reaches_civitai_and_stays_local(
    fake, ready_post
):
    image = post_store.images(ready_post)[0]
    post_store.set_image_flag(image["id"], "hide_meta", True)

    run_push(ready_post)

    remote_id = post_store.get(ready_post)["remote_post_id"]
    remote_image = fake.posts[remote_id]["images"][0]
    assert fake.image_updates == [{"id": remote_image["id"], "hide_meta": True}]
    assert max(i for i, call in enumerate(fake.calls) if call == "post.addImage") < (
        fake.calls.index("post.updateImage")
    )
    assert remote_image["hideMeta"] is True
    assert post_store.images(ready_post)[0]["hide_meta"] is True


def test_hide_meta_on_an_attached_image_still_updates_civitai(fake, ready_post, client):
    run_push(ready_post)
    image = post_store.images(ready_post)[0]

    response = client.post(
        f"/api/posts/{ready_post}/images/{image['id']}/hide-meta?value=true"
    )

    assert response.status_code == 200
    assert fake.image_updates == [{"id": image["remote_image_id"], "hide_meta": True}]
    assert post_store.images(ready_post)[0]["hide_meta"] is True


def test_hide_meta_update_failure_is_retried_after_all_attachments(fake, ready_post):
    image = post_store.images(ready_post)[0]
    post_store.set_image_flag(image["id"], "hide_meta", True)
    fake.fail_at = "update_image"

    run_push(ready_post)

    assert len(fake.images_added) == 2
    assert post_store.images(ready_post)[0]["hide_meta"] is True

    fake.fail_at = None
    run_push(ready_post)

    assert len(fake.images_added) == 2
    assert len(fake.image_updates) == 2
    assert post_store.images(ready_post)[0]["hide_meta"] is True


def test_push_action_logging_defaults_to_off(fake, ready_post, client):
    assert client.get("/api/settings").json()["debug_logging"] is False

    run_push(ready_post)

    assert action_events() == []


def test_debug_logging_records_only_structured_push_outcomes(
    fake, ready_post, client
):
    title = "private-title-for-action-log-test"
    description = "private-description-for-action-log-test"
    client.patch(
        f"/api/posts/{ready_post}",
        json={"title": title, "detail": description},
    )
    saved = client.put("/api/settings", json={"debug_logging": True})
    assert saved.json()["debug_logging"] is True

    run_push(ready_post)

    entries = action_events()
    expected_stages = {
        "push.create_remote",
        "push.tag_sync",
        "push.attach_images",
        "push.publish_time",
        "push.post_update",
    }
    assert {entry["stage"] for entry in entries} == expected_stages
    assert {
        (entry["stage"], entry["detail"]["outcome"]) for entry in entries
    } == {
        (stage, outcome)
        for stage in expected_stages
        for outcome in ("started", "success")
    }
    assert all(entry["timestamp"].endswith("Z") for entry in entries)
    assert all(entry["post_id"] == ready_post for entry in entries)
    assert len({entry["detail"]["run_id"] for entry in entries}) == 1

    remote_id = post_store.get(ready_post)["remote_post_id"]
    completed_create = next(
        entry
        for entry in entries
        if entry["stage"] == "push.create_remote"
        and entry["detail"]["outcome"] == "success"
    )
    assert completed_create["detail"]["remote_post_id"] == remote_id
    assert all(
        entry["detail"]["remote_post_id"] == remote_id
        for entry in entries
        if entry["stage"] != "push.create_remote"
    )

    retained = "\n".join(
        path.read_text(encoding="utf-8")
        for path in config.data_dir().glob(f"{config.DIAGNOSTIC_LOG_FILENAME}*")
    )
    prompts = [
        image["meta"]["prompt"]
        for image in fake.posts[remote_id]["images"]
        if image.get("meta", {}).get("prompt")
    ]
    assert title not in retained
    assert description not in retained
    assert all(prompt not in retained for prompt in prompts)


def test_debug_logging_records_a_step_failure_without_its_message(
    fake, ready_post, client
):
    response_body = "private-civitai-response-body-for-action-log-test"
    client.put("/api/settings", json={"debug_logging": True})
    fake.fail_at = "create"
    fake.failure_detail = response_body

    run_push(ready_post)

    entries = action_events()
    assert [entry["detail"]["outcome"] for entry in entries] == [
        "started",
        "failure",
    ]
    assert all(entry["stage"] == "push.create_remote" for entry in entries)
    assert entries[-1]["detail"]["error_type"] == "CivitaiError"
    retained = "\n".join(
        path.read_text(encoding="utf-8")
        for path in config.data_dir().glob(f"{config.DIAGNOSTIC_LOG_FILENAME}*")
    )
    assert "connection dropped" not in retained
    assert response_body not in retained


def test_create_image_payload_omits_unknown_metadata_instead_of_null():
    payload = push._create_image_payload(
        {
            "remote_uuid": "00000000-0000-0000-0000-000000000001",
            "source_path": "/tmp/example.png",
            "blurhash": "LEHV6nWB2yk8pyo0adR*.7kCMdnj",
            "width": None,
            "height": 1520,
            "file_size": 2_593_800,
            "content_type": "image/png",
        },
        0,
    )

    assert payload["metadata"] == {
        "hash": "LEHV6nWB2yk8pyo0adR*.7kCMdnj",
        "size": 2_593_800,
        "height": 1520,
    }
    assert all(value is not None for value in payload["metadata"].values())

    without_metadata = push._create_image_payload(
        {
            "remote_uuid": "00000000-0000-0000-0000-000000000002",
            "source_path": "/tmp/example.png",
        },
        1,
    )
    assert "metadata" not in without_metadata
    assert "meta" not in without_metadata


def test_the_reconcile_marker_is_attached_and_removed_again(fake, ready_post):
    run_push(ready_post)

    sent_tags = fake.created[0]["tags"]
    marker = [tag for tag in sent_tags if tag.startswith("livebound-r")]
    assert marker, "the marker has to go along at creation"
    assert fake.tags_removed, "and be removed again afterwards"
    assert not [
        row for row in post_store.tags(ready_post, include_transient=True)
        if row["is_transient"]
    ]
    remote_id = post_store.get(ready_post)["remote_post_id"]
    remaining = set(fake.posts[remote_id]["tags"])
    assert not any(name.startswith("livebound-r") for name in remaining), (
        "the marker must not be left behind on CivitAI"
    )
    assert {"alpha", "beta"} <= remaining, "the real tags have to stay"


def test_the_binding_is_frozen_at_creation(fake, client, scanned):
    post = client.post(
        "/api/posts",
        json={"image_ids": scanned, "model_version_id": 1234, "schedule_offset_minutes": 120},
    ).json()
    run_push(post["id"])

    assert fake.created[0]["model_version_id"] == 1234
    assert post_store.get(post["id"])["bound_model_version_id"] == 1234

    # and cannot be changed afterwards
    response = client.patch(f"/api/posts/{post['id']}", json={"model_version_id": 9999})
    assert response.status_code == 409


def test_patching_an_absolute_time_reschedules_the_remote_post(fake, client, ready_post):
    run_push(ready_post)
    remote_id = post_store.get(ready_post)["remote_post_id"]
    later = schedule.iso_z(schedule.utcnow() + timedelta(days=2))
    fake.updates.clear()

    response = client.patch(
        f"/api/posts/{ready_post}",
        json={"scheduled_at": later},
    )

    assert response.status_code == 200
    assert fake.updates == [{"id": remote_id, "published_at": later}]
    returned = response.json()
    assert returned["scheduled_at"] == later
    assert returned["remote_published_at"] == later
    assert returned["resolved_publish_at"] == later


def test_a_refused_time_does_not_land_in_the_row(fake, client, ready_post):
    """LIF-19: after a hard stop the maintainer sets a new time - so the row has
    to still hold the one they can correct, not the one that was thrown out."""
    run_push(ready_post)
    before = post_store.get(ready_post)
    fake.updates.clear()
    too_soon = schedule.iso_z(schedule.utcnow() + timedelta(minutes=5))

    response = client.patch(f"/api/posts/{ready_post}", json={"scheduled_at": too_soon})

    assert response.status_code == 400
    after = post_store.get(ready_post)
    assert after["scheduled_at"] == before["scheduled_at"]
    assert after["schedule_mode"] == before["schedule_mode"]
    assert fake.updates == [], "and nothing was sent to CivitAI either"


def test_patching_both_schedule_forms_refuses_to_choose_one(fake, client, ready_post):
    run_push(ready_post)
    before = post_store.get(ready_post)
    fake.updates.clear()

    response = client.patch(
        f"/api/posts/{ready_post}",
        json={
            "scheduled_at": schedule.iso_z(schedule.utcnow() + timedelta(days=2)),
            "schedule_offset_minutes": 240,
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "schedule_time_and_offset"
    assert post_store.get(ready_post) == before
    assert fake.updates == []


def test_patching_a_relative_offset_reschedules_the_remote_post(fake, client, ready_post):
    run_push(ready_post)
    remote_id = post_store.get(ready_post)["remote_post_id"]
    fake.updates.clear()

    response = client.patch(
        f"/api/posts/{ready_post}",
        json={"schedule_offset_minutes": 240},
    )

    assert response.status_code == 200
    assert len(fake.updates) == 1
    sent = fake.updates[0]
    assert sent["id"] == remote_id
    returned = response.json()
    assert returned["schedule_offset_minutes"] == 240
    assert returned["remote_published_at"] == sent["published_at"]
    assert returned["resolved_publish_at"] == sent["published_at"]


def test_patching_only_the_schedule_mode_stays_local(fake, client, ready_post):
    run_push(ready_post)
    fake.updates.clear()

    response = client.patch(
        f"/api/posts/{ready_post}",
        json={"schedule_mode": "absolute"},
    )

    assert response.status_code == 200
    assert response.json()["schedule_mode"] == "absolute"
    assert fake.updates == []


@pytest.mark.parametrize(
    ("payload", "field", "expected"),
    [
        ({"scheduled_at": "2099-01-02T03:04:05.000Z"}, "scheduled_at", "2099-01-02T03:04:05.000Z"),
        ({"schedule_offset_minutes": 240}, "schedule_offset_minutes", 240),
    ],
)
def test_patching_a_local_schedule_does_not_call_civitai(
    fake, client, payload, field, expected
):
    post = client.post("/api/posts", json={}).json()

    response = client.patch(f"/api/posts/{post['id']}", json=payload)

    assert response.status_code == 200
    assert response.json()[field] == expected
    assert fake.updates == []


def test_patching_a_published_post_returns_the_reschedule_error(fake, client, ready_post):
    run_push(ready_post)
    fake.updates.clear()
    post_store.set_fields(ready_post, state=lifecycle.PUBLISHED)

    response = client.patch(
        f"/api/posts/{ready_post}",
        json={"schedule_offset_minutes": 240},
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == lifecycle.ERROR_RESCHEDULE_PUBLISHED
    assert response.json()["detail"]["params"] == {
        "current": lifecycle.PUBLISHED,
        "target": lifecycle.SCHEDULED,
    }
    assert fake.updates == []


# --- interruptions ----------------------------------------------------------


def test_an_interrupted_upload_resumes_without_re_uploading(fake, ready_post, scanned):
    fake.fail_at = "upload"
    run_push(ready_post)
    assert fake.uploads == []
    post = post_store.get(ready_post)
    assert post["state"] == lifecycle.REMOTE_DRAFT
    assert post["remote_published_at"] is None
    assert "2 of 2 images are missing" in post["last_error"]
    assert "still a draft" in post["last_error"]

    fake.fail_at = None
    run_push(ready_post)
    assert len(fake.uploads) == len(scanned)

    # a third run must not upload again - the uuids are still fresh
    before = len(fake.uploads)
    run_push(ready_post)
    assert len(fake.uploads) == before


def test_a_lost_create_response_never_creates_a_second_post(fake, ready_post, monkeypatch):
    """The dangerous one. The post may exist on CivitAI; a retry must look for
    it rather than make another."""
    fake.fail_at = "create"
    run_push(ready_post)

    post = post_store.get(ready_post)
    assert post["remote_post_id"] is None
    assert post["state"] == lifecycle.FAILED

    # the attempt was recorded before the call went out
    run_id = db.get_connection().execute(
        "SELECT run_id FROM push_items WHERE post_id=? ORDER BY id DESC LIMIT 1", (ready_post,)
    ).fetchone()["run_id"]
    item = run_store.get_item(run_id, ready_post)
    assert item["create_attempted_at"] is not None
    assert item["step"] == run_store.CREATE_ATTEMPTED

    # resuming finds nothing on the remote side and refuses to create blindly
    monkeypatch.setattr(
        push.reconcile, "auto_adopt",
        lambda post_id, run_id=None: {"adopted": None, "candidates": [], "reason": "nothing"},
    )
    fake.fail_at = None
    lifecycle.transition(ready_post, lifecycle.PUSHING)
    run_store.set_item(run_id, ready_post, step=run_store.CREATE_ATTEMPTED)
    with pytest.raises(RuntimeError):
        push.push_one(run_id, ready_post)

    assert fake.created == [], "no second post may come into being"
    assert post_store.get(ready_post)["state"] == lifecycle.NEEDS_RECONCILE


def test_a_create_transport_error_reaches_reconcile_without_an_internal_retry(
    fake, ready_post, monkeypatch
):
    response = httpx.Response(
        200,
        json={"result": {"data": {"json": {"id": 5001, "publishedAt": None}}}},
    )
    fake.http_outcomes = [httpx.ReadError("response lost"), response]
    monkeypatch.setattr(push.trpc, "post_create", _REAL_POST_CREATE)
    monkeypatch.setattr(civitai_client.httpx, "Client", lambda **kwargs: fake)
    # The pool is process-wide and may already hold a real client from setting
    # the post up; dropping it makes the next call build the stand-in instead.
    civitai_client.close_http_client()
    monkeypatch.setattr(civitai_client, "require_token", lambda: "test-token")

    run_push(ready_post)

    first_run_id = db.get_connection().execute(
        "SELECT run_id FROM push_items WHERE post_id=? ORDER BY id DESC LIMIT 1",
        (ready_post,),
    ).fetchone()["run_id"]
    first_item = run_store.get_item(first_run_id, ready_post)
    assert fake.http_calls == 1
    assert first_item["create_attempted_at"] is not None
    assert first_item["step"] == run_store.CREATE_ATTEMPTED

    monkeypatch.setattr(
        push.reconcile,
        "auto_adopt",
        lambda post_id, run_id=None: {
            "adopted": None,
            "candidates": [],
            "reason": "the create outcome is uncertain",
        },
    )
    run_push(ready_post)

    assert fake.http_calls == 1, "post.create must not be attempted again"
    assert post_store.get(ready_post)["state"] == lifecycle.NEEDS_RECONCILE
    assert run_store.get_item(first_run_id, ready_post)["create_attempted_at"] is not None


def test_an_ordinary_post_json_call_still_retries_a_transport_error(fake, monkeypatch):
    response = httpx.Response(200, json=[{"id": 7}])
    fake.http_outcomes = [httpx.ReadError("temporary reset"), response]
    monkeypatch.setattr(civitai_client.httpx, "Client", lambda **kwargs: fake)
    civitai_client.close_http_client()
    monkeypatch.setattr(civitai_client.time, "sleep", lambda seconds: None)

    result = civitai_client.post_json(
        "https://example.invalid/model-versions/by-hash",
        ["A" * 64],
        procedure="model-versions/by-hash",
        transport="rest",
        authenticated=False,
        response_type=list,
    )

    assert result == [{"id": 7}]
    assert fake.http_calls == 2


def test_a_batch_of_calls_shares_one_connection_pool(monkeypatch):
    """A push makes dozens of requests to the same host. A client per call
    repeats DNS, TCP and TLS setup every time - more work than the request."""
    built = []

    class Pool:
        def __init__(self, **kwargs):
            self.closed = False
            built.append(self)

        def post(self, url, *, json, headers):
            return httpx.Response(200, json={"ok": True})

        def get(self, url, *, params, headers):
            return httpx.Response(200, json={"ok": True})

        def close(self):
            self.closed = True

    monkeypatch.setattr(civitai_client.httpx, "Client", Pool)
    civitai_client.close_http_client()

    for _ in range(3):
        civitai_client.post_json(
            "https://example.invalid/api",
            {},
            procedure="post.get",
            transport="rest",
            authenticated=False,
        )
    civitai_client.get_json(
        "https://example.invalid/api",
        procedure="post.get",
        transport="rest",
        authenticated=False,
    )

    assert len(built) == 1, "four calls, one pool"

    civitai_client.close_http_client()
    assert built[0].closed, "shutdown has to release the pool"


def test_a_retry_exhausted_transport_error_is_logged(fake, monkeypatch):
    fake.http_outcomes = [
        httpx.ReadError("reset one"),
        httpx.ReadError("reset two"),
        httpx.ReadError("reset three"),
    ]
    monkeypatch.setattr(civitai_client.httpx, "Client", lambda **kwargs: fake)
    civitai_client.close_http_client()
    monkeypatch.setattr(civitai_client.time, "sleep", lambda seconds: None)
    db.set_setting("debug_logging", "1")

    with pytest.raises(TransportError, match="reset three"):
        civitai_client.post_json(
            "https://example.invalid/model-versions/by-hash",
            ["A" * 64],
            procedure="model-versions/by-hash",
            transport="rest",
            authenticated=False,
            response_type=list,
        )

    logged = db.get_connection().execute(
        "SELECT * FROM api_calls WHERE procedure='model-versions/by-hash'"
    ).fetchone()
    assert fake.http_calls == 3
    assert logged is not None
    assert logged["http_status"] is None
    assert logged["ok"] == 0


def test_reconcile_adopts_the_post_that_was_actually_created(fake, ready_post, monkeypatch):
    fake.fail_at = "create"
    run_push(ready_post)
    run_id = db.get_connection().execute(
        "SELECT run_id FROM push_items WHERE post_id=? ORDER BY id DESC LIMIT 1", (ready_post,)
    ).fetchone()["run_id"]

    def adopt(post_id, run_id=None):
        # The post really is on CivitAI - that is the whole premise of a
        # reconcile - so the fake has to hold it, marker tag and all.
        item = run_store.get_item(run_id, post_id) if run_id else None
        marker = (item or {}).get("reconcile_tag") or "livebound-r1-p1"
        fake.posts[6001] = {
            "id": 6001,
            "title": "Testpost",
            "detail": "",
            "publishedAt": "2099-01-01T00:00:00.000Z",
            "tags": {marker.lower(): 900, "alpha": 901, "beta": 902},
            "images": [],
        }
        post_store.set_fields(
            post_id,
            remote_post_id=6001,
            remote_published_at="2099-01-01T00:00:00.000Z",
            remote_state="scheduled",
        )
        return {"adopted": {"remote_post_id": 6001}, "candidates": [], "reason": ""}

    monkeypatch.setattr(push.reconcile, "auto_adopt", adopt)
    fake.fail_at = None
    lifecycle.transition(ready_post, lifecycle.PUSHING)
    run_store.set_item(run_id, ready_post, step=run_store.CREATE_ATTEMPTED)
    push.push_one(run_id, ready_post)

    assert fake.created == [], "the existing post is adopted, not created again"
    assert post_store.get(ready_post)["remote_post_id"] == 6001
    # and the marker left behind by the lost attempt gets cleaned up
    assert not any(name.startswith("livebound-r") for name in fake.posts[6001]["tags"])


def test_a_stuck_marker_tag_stops_before_uploading(fake, ready_post):
    fake.fail_at = "untag"
    run_push(ready_post)

    assert fake.uploads == []
    post = post_store.get(ready_post)
    assert post["remote_post_id"] is not None
    assert post["state"] == lifecycle.REMOTE_DRAFT
    assert "2 of 2 images are missing" in post["last_error"]
    assert "still a draft" in post["last_error"]

    fake.fail_at = None
    run_push(ready_post)

    assert len(fake.created) == 1
    assert len(fake.uploads) == 2
    assert all(image["remote_image_id"] for image in post_store.images(ready_post))


def test_an_unreadable_tag_list_also_stops_before_publishing(fake, ready_post):
    """Not being able to check is not the same as being safe: if the marker
    cannot be confirmed gone, the post stays a draft."""
    fake.fail_at = "post_get"
    run_push(ready_post)

    assert fake.uploads == []
    assert fake.updates == []
    assert post_store.get(ready_post)["state"] == lifecycle.REMOTE_DRAFT


def test_a_create_result_with_the_wrong_schedule_stops_before_upload(
    fake, ready_post, monkeypatch
):
    real_create = fake.post_create

    def wrong_schedule(**kwargs):
        result = real_create(**kwargs)
        result["publishedAt"] = "2026-01-01T00:00:00.000Z"
        fake.posts[result["id"]]["publishedAt"] = result["publishedAt"]
        return result

    monkeypatch.setattr(push.trpc, "post_create", wrong_schedule)
    run_push(ready_post)

    post = post_store.get(ready_post)
    assert post["remote_post_id"] is not None
    assert fake.uploads == []
    assert post["last_error"]


def test_startup_recovery_marks_a_possible_create_for_reconciliation(fake, ready_post):
    fake.fail_at = "create"
    run_push(ready_post)

    # simulate the process dying mid-push
    with db.transaction() as conn:
        conn.execute("UPDATE posts SET state='pushing' WHERE id=?", (ready_post,))
        conn.execute("UPDATE push_runs SET status='running'")

    recovered = run_store.recover_interrupted()
    assert recovered["needs_reconcile"] == 1
    assert post_store.get(ready_post)["state"] == lifecycle.NEEDS_RECONCILE


# --- preflight --------------------------------------------------------------


def test_a_changed_source_file_blocks_the_push(fake, ready_post, fixture_images):
    """The plan was made for specific bytes; uploading different ones would
    publish something the user never reviewed."""
    _, images = fixture_images
    images[0].write_bytes(images[0].read_bytes() + b"\x00trailing")

    run_push(ready_post)
    assert fake.created == []
    assert "has changed" in (post_store.get(ready_post)["last_error"] or "")


def test_a_missing_source_file_blocks_the_push(fake, ready_post, fixture_images):
    _, images = fixture_images
    images[0].unlink()

    run_push(ready_post)
    assert fake.created == []
    assert post_store.get(ready_post)["state"] == lifecycle.FAILED


def test_a_dry_run_sends_nothing(fake, ready_post):
    job = jobs.Job(id=0, kind="push")
    push.push_posts(job, [ready_post], dry_run=True)

    assert fake.uploads == [] and fake.created == [] and fake.updates == []
    assert job.result["preview"][0]["can_push"] is True


def test_preview_uses_stored_sizes_without_materialising_ordinary_images(
    ready_post, monkeypatch
):
    images = post_store.images(ready_post)
    threshold = (
        max(image["file_size"] for image in images)
        + push._PREVIEW_UPLOAD_ROUTE_MARGIN_BYTES
        + 1
    )
    monkeypatch.setattr(push.config, "MCP_UPLOAD_MAX_BYTES", threshold)

    def unexpected_materialise(*args, **kwargs):
        raise AssertionError("an ordinary preview must not copy image bytes")

    monkeypatch.setattr(push.materialise, "path_for", unexpected_materialise)

    upload_calls = [
        call for call in push.preview(ready_post)["calls"] if call["call"] == "upload_image"
    ]
    assert len(upload_calls) == len(images)


def test_preview_names_images_whose_comfyui_workflows_are_replaced(ready_post, monkeypatch):
    image = post_store.images(ready_post)[0]
    monkeypatch.setattr(
        push.materialise,
        "comfyui_workflow_replaced",
        lambda row, **_kwargs: row["image_id"] == image["image_id"],
    )

    preview = push.preview(ready_post)

    assert preview["comfyui_workflow_replaced_images"] == [Path(image["source_path"]).name]


def test_preview_materialises_an_image_near_the_route_boundary(ready_post, monkeypatch):
    image = post_store.images(ready_post)[0]
    monkeypatch.setattr(push.config, "MCP_UPLOAD_MAX_BYTES", image["file_size"])
    materialised_sizes: dict[str, int] = {}
    real_path_for = push.materialise.path_for

    def record_size(image, directory, text):
        path = real_path_for(image, directory, text)
        materialised_sizes[Path(image["source_path"]).name] = path.stat().st_size
        return path

    monkeypatch.setattr(push.materialise, "path_for", record_size)
    calls = push.preview(ready_post)["calls"]

    name = Path(image["source_path"]).name
    upload = next(
        call
        for call in calls
        if call["detail"] == name and call["call"] in {"upload_image", "upload_presigned"}
    )
    expected = (
        "upload_image"
        if materialised_sizes[name] <= push.config.MCP_UPLOAD_MAX_BYTES
        else "upload_presigned"
    )
    assert upload["call"] == expected


def test_the_planned_calls_are_the_calls_the_pipeline_issues(
    fake, ready_post, monkeypatch
):
    image = post_store.images(ready_post)[0]
    post_store.set_image_flag(image["id"], "hide_meta", True)

    real_path_for = push.materialise.path_for

    def record_materialise(image, directory, text):
        fake.calls.append("materialise_copy")
        return real_path_for(image, directory, text)

    monkeypatch.setattr(push.materialise, "path_for", record_materialise)
    planned = [call["call"] for call in push.preview(ready_post)["calls"]]
    fake.calls.clear()

    def confirmation(post_id):
        post = post_store.get(post_id) or {}
        return {"published_at": post.get("remote_published_at")}

    monkeypatch.setattr(push.sync, "sync_one", confirmation)
    monkeypatch.setattr(push.sync, "snapshot_pushed", lambda post_id: None)
    run_push(ready_post)

    assert planned == fake.calls


def test_an_edited_image_is_materialised_for_upload_and_then_removed(
    fake, ready_post, scanned
):
    from backend.hashing import file_sha256

    assert len(scanned) >= 2
    rows = {row["image_id"]: row for row in post_store.images(ready_post)}
    edited = rows[scanned[0]]
    source = Path(edited["source_path"])
    original_hash = file_sha256(source)
    edits.save(
        scanned[0],
        draft={"prompt": "an edited lighthouse", "negative_prompt": None, "fields": {}},
        touched=["prompt"],
        deleted=[],
    )

    preview = push.preview(ready_post)
    assert any(
        call["call"] == "materialise_copy"
        and call["detail"] == source.name
        and call["changed"] is True
        for call in preview["calls"]
    )

    run_push(ready_post)

    edited_upload = next(
        (path, payload)
        for path, payload in zip(fake.uploads, fake.uploaded_bytes, strict=True)
        if Path(path).name.endswith(source.name) and Path(path) != source
    )
    assert b"an edited lighthouse" in edited_upload[1]
    assert file_sha256(source) == original_hash
    assert not Path(edited_upload[0]).exists()
    stored = next(
        row for row in post_store.images(ready_post) if row["image_id"] == scanned[0]
    )
    assert stored["sha256"] == original_hash


def test_an_untouched_image_uploads_a_copy_with_the_original_compressed_pixels(
    fake, ready_post, scanned
):
    from backend.metadata import write

    row = next(item for item in post_store.images(ready_post) if item["image_id"] == scanned[0])
    source = Path(row["source_path"])

    run_push(ready_post)

    upload_path, upload_bytes = next(
        (Path(path), payload)
        for path, payload in zip(fake.uploads, fake.uploaded_bytes, strict=True)
        if Path(path).name.endswith(source.name)
    )
    source_pixels = b"".join(
        payload for kind, payload in write._png_chunks(source.read_bytes()) if kind == b"IDAT"
    )
    uploaded_pixels = b"".join(
        payload for kind, payload in write._png_chunks(upload_bytes) if kind == b"IDAT"
    )

    assert upload_path != source
    assert uploaded_pixels == source_pixels
    assert not upload_path.exists()


def test_a_staged_original_upload_is_not_reused_after_an_edit(
    fake, client, ready_post, scanned
):
    second = client.post(
        "/api/posts",
        json={"image_ids": [scanned[0]], "schedule_offset_minutes": 120},
    ).json()["id"]
    fake.fail_at = "add_image"
    run_push(ready_post)
    uploads_before_edit = len(fake.uploads)

    edits.save(
        scanned[0],
        draft={"prompt": "the edited variant", "negative_prompt": None, "fields": {}},
        touched=["prompt"],
        deleted=[],
    )
    fake.fail_at = None
    run_push(second)

    assert len(fake.uploads) == uploads_before_edit + 1
    assert b"the edited variant" in fake.uploaded_bytes[-1]


def test_an_edited_temporary_copy_is_removed_when_upload_fails(
    fake, ready_post, scanned, monkeypatch
):
    edits.save(
        scanned[0],
        draft={"prompt": "temporary", "negative_prompt": None, "fields": {}},
        touched=["prompt"],
        deleted=[],
    )
    attempted: list[Path] = []

    def fail(path: Path, content_type: str | None):
        attempted.append(path)
        raise CivitaiError("upload failed")

    monkeypatch.setattr(push, "_upload", fail)
    run_push(ready_post)

    assert attempted
    assert all(not path.exists() for path in attempted)


def test_a_reused_upload_keeps_its_byte_size_after_the_source_post_is_reordered(
    fake, client, ready_post, scanned
):
    assert len(scanned) >= 2
    edits.save(
        scanned[1],
        draft={
            "prompt": "a substantially different metadata variant " * 100,
            "negative_prompt": None,
            "fields": {},
        },
        touched=["prompt"],
        deleted=[],
    )
    second = client.post(
        "/api/posts",
        json={"image_ids": [scanned[1]], "schedule_offset_minutes": 120},
    ).json()["id"]

    fake.fail_at = "add_image"
    run_push(ready_post)
    uploads_after_first = len(fake.uploads)
    reused_name = Path(
        next(
            row["source_path"]
            for row in post_store.images(ready_post)
            if row["image_id"] == scanned[1]
        )
    ).name
    uploaded_size = next(
        len(payload)
        for path, payload in zip(fake.uploads, fake.uploaded_bytes, strict=True)
        if Path(path).name.endswith(reused_name)
    )
    source_size = next(
        Path(row["source_path"]).stat().st_size
        for row in post_store.images(ready_post)
        if row["image_id"] == scanned[1]
    )
    assert uploaded_size != source_size, "the test needs distinct source and upload bytes"

    post_store.set_images(ready_post, list(reversed(scanned)))
    fake.fail_at = None

    run_push(second)

    assert len(fake.uploads) == uploads_after_first
    remote_id = post_store.get(second)["remote_post_id"]
    assert fake.posts[remote_id]["images"][0]["sizeKB"] == round(uploaded_size / 1024)


def test_schema_eleven_adds_the_uploaded_file_size_column():
    from backend import db

    conn = db.get_connection()
    conn.execute("DROP TABLE upload_assets")
    conn.execute(
        """
        CREATE TABLE upload_assets (
            sha256 TEXT NOT NULL,
            uuid TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            content_type TEXT,
            uploaded_at TEXT NOT NULL,
            consumed_by_post_id INTEGER,
            PRIMARY KEY(sha256, uuid)
        )
        """
    )
    conn.execute("PRAGMA user_version=10")
    conn.commit()

    db.init_db()

    columns = {row["name"] for row in conn.execute("PRAGMA table_info(upload_assets)")}
    assert "file_size" in columns
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


# --- divergence -------------------------------------------------------------


def test_a_push_records_what_civitai_holds(fake, ready_post):
    """Without this baseline the divergence detection can never fire, because
    it compares against "what we last sent"."""
    run_push(ready_post)

    post = post_store.get(ready_post)
    baseline = post["pushed_fields"]
    assert baseline, "a baseline has to exist after the push"
    assert baseline["title"] == "Testpost"
    assert set(baseline["tags"]) == {"alpha", "beta"}
    assert post["dirty"] is False


def test_text_push_records_the_confirmed_remote_baseline(fake, ready_post, client):
    run_push(ready_post)
    client.patch(
        f"/api/posts/{ready_post}",
        json={"title": "Changed title", "detail": "Changed description"},
    )

    response = client.post(f"/api/posts/{ready_post}/push-text")

    assert response.status_code == 200
    post = post_store.get(ready_post)
    remote = fake.posts[post["remote_post_id"]]
    assert post["pushed_fields"] == {
        "title": remote["title"],
        "detail": remote["detail"],
        "publishedAt": remote["publishedAt"],
        "tags": ["alpha", "beta"],
    }
    assert post["dirty"] is False


def test_refused_empty_description_stays_local_and_ends_the_loop(
    fake, ready_post, client
):
    run_push(ready_post)
    client.patch(f"/api/posts/{ready_post}", json={"detail": "Keep this remotely"})
    assert client.post(f"/api/posts/{ready_post}/push-text").status_code == 200

    remote_id = post_store.get(ready_post)["remote_post_id"]
    fake.posts[remote_id]["tags"]["spam"] = 999
    from backend.posts import sync

    sync.sync_one(ready_post)
    assert post_store.get(ready_post)["remote_diverged"] is True

    client.patch(f"/api/posts/{ready_post}", json={"detail": ""})
    fake.ignore_empty_detail = True

    response = client.post(f"/api/posts/{ready_post}/push-text")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "post_text_update_not_confirmed"
    post = post_store.get(ready_post)
    assert post["detail"] == ""
    assert post["dirty"] is True
    assert post["remote_diverged"] is True
    assert post["pushed_fields"]["detail"] == "Keep this remotely"
    assert set(post["pushed_fields"]["tags"]) == {"alpha", "beta"}
    assert "publishedAt" in post["pushed_fields"]
    assert "spam" in fake.posts[remote_id]["tags"]


@pytest.mark.parametrize("failure", ["post_get", "post_get_shape"])
def test_text_push_reports_an_unconfirmed_read_separately(
    fake, ready_post, client, failure
):
    run_push(ready_post)
    previous = post_store.get(ready_post)["pushed_fields"]
    client.patch(f"/api/posts/{ready_post}", json={"detail": "Sent but unread"})
    fake.fail_at = failure

    response = client.post(f"/api/posts/{ready_post}/push-text")

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "post_text_update_confirmation_failed"
    post = post_store.get(ready_post)
    assert post["pushed_fields"] == previous
    assert post["dirty"] is True


def test_adopting_a_coarse_snapshot_cannot_clear_the_local_description(
    fake, ready_post, client
):
    run_push(ready_post)
    previous = post_store.get(ready_post)["pushed_fields"]
    client.patch(f"/api/posts/{ready_post}", json={"detail": "Keep this locally"})
    post_store.set_fields(
        ready_post,
        remote_diverged=1,
        remote_snapshot_json=json.dumps(
            {"id": 5001, "title": "Remote title", "publishedAt": None}
        ),
    )

    response = client.post(f"/api/posts/{ready_post}/adopt-remote-snapshot")

    assert response.status_code == 200
    post = post_store.get(ready_post)
    assert post["title"] == "Remote title"
    assert post["detail"] == "Keep this locally"
    assert post["pushed_fields"]["detail"] == previous["detail"]
    assert post["pushed_fields"]["tags"] == previous["tags"]
    assert post["dirty"] is False
    assert post["remote_diverged"] is False


def test_an_edit_made_on_civitai_is_noticed(fake, ready_post):
    from backend.posts import sync

    run_push(ready_post)
    remote_id = post_store.get(ready_post)["remote_post_id"]

    # somebody renames the post on the website
    fake.posts[remote_id]["title"] = "Renamed on the site"
    sync.sync_one(ready_post)

    assert post_store.get(ready_post)["remote_diverged"] is True


def test_an_unchanged_post_does_not_report_divergence(fake, ready_post):
    from backend.posts import sync

    run_push(ready_post)
    sync.sync_one(ready_post)
    assert post_store.get(ready_post)["remote_diverged"] is False


def test_a_tag_removed_on_civitai_is_noticed(fake, ready_post):
    from backend.posts import sync

    run_push(ready_post)
    remote_id = post_store.get(ready_post)["remote_post_id"]
    fake.posts[remote_id]["tags"].pop("beta", None)

    sync.sync_one(ready_post)
    assert post_store.get(ready_post)["remote_diverged"] is True


def test_a_coarse_read_does_not_raise_a_false_alarm(fake, ready_post, monkeypatch):
    """The MCP fallback carries no `detail`. Reading that as "the description was
    deleted" would flag every sync as diverged."""
    from backend.civitai.errors import CivitaiError
    from backend.posts import sync

    run_push(ready_post)
    monkeypatch.setattr(
        sync.trpc, "post_get", lambda post_id: (_ for _ in ()).throw(CivitaiError("weg"))
    )
    sync.sync_one(ready_post)

    assert post_store.get(ready_post)["remote_diverged"] is False


# --- adoption is verified, not trusted --------------------------------------


def test_adopting_a_published_post_is_refused(fake, ready_post):
    """A local post is by definition not published yet, so pairing it with a
    published one is always a mistyped id - and it would put a real post within
    reach of the delete button."""
    from backend.posts import reconcile

    fake.posts[7001] = {
        "id": 7001,
        "title": "Something published",
        "detail": "",
        "publishedAt": "2020-01-01T00:00:00Z",
        "tags": {},
    }
    with pytest.raises(ValueError, match="already published"):
        reconcile.adopt(ready_post, 7001)

    assert post_store.get(ready_post)["remote_post_id"] is None


def test_adopting_a_post_that_another_local_post_owns_is_refused(fake, client, scanned):
    from backend.posts import reconcile

    first = client.post("/api/posts", json={"image_ids": scanned}).json()["id"]
    second = client.post("/api/posts", json={"image_ids": scanned}).json()["id"]
    post_store.set_fields(first, remote_post_id=7002)

    fake.posts[7002] = {"id": 7002, "title": "x", "detail": "", "publishedAt": None, "tags": {}}
    with pytest.raises(ValueError, match="already known locally"):
        reconcile.adopt(second, 7002)


def test_adopting_an_unknown_id_is_refused(fake, ready_post):
    from backend.posts import reconcile

    with pytest.raises(ValueError, match="not retrievable"):
        reconcile.adopt(ready_post, 999999)


def test_adopting_a_real_draft_works(fake, ready_post):
    from backend.posts import reconcile

    fake.posts[7003] = {
        "id": 7003,
        "title": "The lost draft",
        "detail": "",
        "publishedAt": None,
        "tags": {},
    }
    result = reconcile.adopt(ready_post, 7003)

    assert result["remote_post_id"] == 7003
    assert post_store.get(ready_post)["remote_post_id"] == 7003


# --- upload transport --------------------------------------------------------


def test_a_normal_image_goes_the_documented_route(fake, ready_post, monkeypatch):
    """base64 via MCP is the documented path and handles a typical render fine."""
    presigned: list[str] = []
    monkeypatch.setattr(
        push.rest, "upload_presigned", lambda path, content_type=None: presigned.append(str(path))
    )
    run_push(ready_post)

    assert fake.uploads, "the MCP route was used"
    assert presigned == [], "and the direct upload was not"


def test_a_large_image_takes_the_direct_route(fake, ready_post, monkeypatch, fixture_images):
    """Past the threshold base64 would inflate the body by a third and have to
    exist as one string in memory on both ends."""
    from backend import config

    monkeypatch.setattr(config, "MCP_UPLOAD_MAX_BYTES", 1024)  # everything is "large"
    monkeypatch.setattr(push.config, "MCP_UPLOAD_MAX_BYTES", 1024)

    calls: list[str] = []

    def presign(path, content_type=None):
        calls.append(str(path))
        return {"uuid": f"presigned-{len(calls)}", "content_type": "image/png"}

    monkeypatch.setattr(push.rest, "upload_presigned", presign)

    upload_calls = [
        call
        for call in push.preview(ready_post)["calls"]
        if call["call"] in {"upload_image", "upload_presigned"}
    ]
    assert upload_calls
    assert all(
        call["transport"] == "rest" and call["call"] == "upload_presigned"
        for call in upload_calls
    )

    run_push(ready_post)

    assert calls, "the direct upload was used"
    assert fake.uploads == [], "and the MCP route was not"
    remote_id = post_store.get(ready_post)["remote_post_id"]
    assert all(image.get("width") for image in fake.posts[remote_id]["images"])


# --- the loading placeholder --------------------------------------------------


def test_the_blurhash_matches_the_previous_encoder(fixture_images):
    """The pure-Python encoder preserves the placeholder already sent to CivitAI."""
    from backend.metadata.preview import blurhash_for

    _, images = fixture_images
    assert blurhash_for(images[0]) == "UYGlCesp2CbYa~E#S1;giNR+$NWVn~WBayWV"


def test_the_blurhash_reaches_civitai(fake, ready_post):
    """Without it the edit view shows an empty grey box where the blurred
    preview belongs - the visible difference between a post made in the browser
    and one made through the API."""
    run_push(ready_post)

    remote_id = post_store.get(ready_post)["remote_post_id"]
    sent = fake.posts[remote_id]["images"]
    assert sent, "the images went along"
    for image in sent:
        assert image.get("hash"), "every image carries a placeholder"
        assert len(image["hash"]) == 36, "4x4 blurhash, the way CivitAI writes it"
        assert image.get("name"), "the filename goes along"
        assert image.get("mimeType")
    assert [image["index"] for image in sent] == list(range(len(sent))), "order stated explicitly"


def test_the_blurhash_is_stored_and_not_recomputed(fake, ready_post):
    run_push(ready_post)
    stored = [image["blurhash"] for image in post_store.images(ready_post)]
    assert all(stored), "the placeholder stays on the image"


def test_a_lost_add_image_response_resumes_without_a_duplicate(fake, ready_post):
    fake.fail_at = "add_image_response"
    run_push(ready_post)

    post = post_store.get(ready_post)
    assert len(fake.images_added) == 1
    assert "1 of 2 images are missing" in post["last_error"]

    fake.fail_at = None
    run_push(ready_post)

    assert len(fake.images_added) == 2, "the landed first image is returned, not duplicated"
    assert all(image["remote_image_id"] for image in post_store.images(ready_post))


def test_an_abort_after_all_attachments_resumes_without_reattaching(
    fake, ready_post, monkeypatch
):
    real_apply = push._apply_publish
    monkeypatch.setattr(
        push,
        "_apply_publish",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("abort after attach")),
    )
    run_push(ready_post)

    assert len(fake.images_added) == 2
    assert all(image["remote_image_id"] for image in post_store.images(ready_post))
    post = post_store.get(ready_post)
    assert post["state"] == lifecycle.REMOTE_DRAFT
    assert post["remote_published_at"] is None
    assert fake.updates == []
    assert "0 of 2 images are missing" in post["last_error"]
    assert "still a draft" in post["last_error"]

    monkeypatch.setattr(push, "_apply_publish", real_apply)
    run_push(ready_post)

    assert len(fake.images_added) == 2
    assert len(fake.updates) == 1
    assert post_store.get(ready_post)["state"] == lifecycle.SCHEDULED


def test_a_lost_publish_time_response_resumes_without_moving_a_relative_time(
    fake, ready_post
):
    fake.fail_at = "update_response"
    run_push(ready_post)

    post = post_store.get(ready_post)
    remote_id = post["remote_post_id"]
    landed_at = fake.posts[remote_id]["publishedAt"]
    assert landed_at
    assert len(fake.updates) == 1

    # Simulate the harder process-crash shape too: the remote write survived,
    # but neither its response nor the local readback did.
    post_store.set_fields(ready_post, remote_published_at=None, remote_state="draft")
    fake.fail_at = None
    run_push(ready_post)

    assert len(fake.updates) == 1, "the landed time is read, not resolved and sent again"
    post = post_store.get(ready_post)
    assert post["remote_published_at"] == landed_at
    assert post["state"] == lifecycle.SCHEDULED


def test_the_remote_image_ids_are_learned_and_stored(fake, ready_post):
    """Needed to address a single image later - hiding its metadata, removing
    it, and naming the file when it is archived."""
    from backend import db as db_module

    run_push(ready_post)

    rows = post_store.images(ready_post)
    assert rows and all(row["remote_image_id"] for row in rows), "every row knows its id"

    # and the dedup history carries it too, which is what the archive reads
    ids = {
        row["remote_image_id"]
        for row in db_module.get_connection().execute(
            "SELECT remote_image_id FROM image_usage WHERE post_id=?", (ready_post,)
        )
    }
    assert ids == {row["remote_image_id"] for row in rows}


def test_the_ids_are_matched_by_upload_key_not_by_position(fake, ready_post, scanned):
    """Positional matching would mislabel the moment CivitAI returns them in a
    different order."""
    run_push(ready_post)
    remote_id = post_store.get(ready_post)["remote_post_id"]

    # the server answers in reverse order
    fake.posts[remote_id]["images"].reverse()
    from backend.posts import sync

    sync.sync_one(ready_post)

    for row in post_store.images(ready_post):
        expected = next(
            image["id"]
            for image in fake.posts[remote_id]["images"]
            if (image.get("url") or image.get("uuid")) == row["remote_uuid"]
        )
        assert row["remote_image_id"] == expected


def test_importing_a_stranger_post_is_refused(fake, monkeypatch):
    """Reading is public, so a mistyped id resolves to somebody else's post.
    Changing it is impossible anyway - CivitAI refuses - but without this check it
    would sit on your own board as if it were yours."""
    from backend.posts import sync

    fake.posts[8001] = {
        "id": 8001,
        "title": "Fremder Post",
        "detail": "",
        "publishedAt": None,
        "tags": {},
        "images": [],
    }
    monkeypatch.setattr(
        sync.trpc,
        "post_get",
        lambda post_id: {**fake.post_get(post_id), "user": {"username": "someone-else"}},
    )
    monkeypatch.setattr(sync.account, "username", lambda: "tester")

    with pytest.raises(ValueError, match="belongs to"):
        sync.import_remote(8001)


@pytest.mark.parametrize(
    ("published_at", "expected_state"),
    [(None, lifecycle.REMOTE_DRAFT), ("2020-01-01T00:00:00Z", lifecycle.PUBLISHED)],
)
def test_importing_an_own_post_uses_its_remote_state(
    fake, client, monkeypatch, published_at, expected_state
):
    from backend.posts import sync

    fake.posts[8002] = {
        "id": 8002,
        "title": "Own post",
        "detail": "",
        "publishedAt": published_at,
        "tags": {},
        "images": [],
    }
    monkeypatch.setattr(
        sync.trpc,
        "post_get",
        lambda post_id: {**fake.post_get(post_id), "user": {"username": "Tester"}},
    )
    monkeypatch.setattr(sync.account, "username", lambda: "tester")

    result = sync.import_remote(8002)
    assert result["created"] is True
    post = post_store.get(result["post_id"])
    assert post["remote_post_id"] == 8002
    assert post["state"] == expected_state
    assert lifecycle.INFO[post["state"]].can_edit_remote is True
    assert client.get(f"/api/posts/{post['id']}").json()["can_edit_remote"] is True


# --- images that exist only on CivitAI ----------------------------------------


def test_an_adopted_post_shows_its_images(fake, monkeypatch):
    """Without these rows an adopted post would stand there empty, although it
    visibly has images."""
    from backend.posts import sync

    fake.posts[8100] = {
        "id": 8100,
        "title": "Adopted draft",
        "detail": "",
        "publishedAt": None,
        "tags": {},
        "images": [{"url": "uuid-remote-1"}, {"url": "uuid-remote-2"}],
    }
    monkeypatch.setattr(
        sync.trpc, "post_get", lambda pid: {**fake.post_get(pid), "user": {"username": "t"}}
    )
    monkeypatch.setattr(sync.account, "username", lambda: "t")

    result = sync.import_remote(8100)
    images = post_store.images(result["post_id"])

    assert len(images) == 2, "the post's images are there"
    for row in images:
        assert row["image_id"] is None, "there is no local file"
        assert row["remote_image_id"], "but the CivitAI id is"
        assert row["remote_url"].startswith("https://image.civitai.com/"), "and an address"


def test_an_adopted_post_does_not_need_local_files_to_be_rescheduled(fake, monkeypatch):
    """The images already hang on the post - there is nothing to upload and no
    file that would be needed for it."""
    from backend.posts import sync, validation

    fake.posts[8101] = {
        "id": 8101,
        "title": "Adopted",
        "detail": "",
        "publishedAt": None,
        "tags": {},
        "images": [{"url": f"uuid-remote-{index}"} for index in range(21)],
    }
    monkeypatch.setattr(
        sync.trpc, "post_get", lambda pid: {**fake.post_get(pid), "user": {"username": "t"}}
    )
    monkeypatch.setattr(sync.account, "username", lambda: "t")
    post_id = sync.import_remote(8101)["post_id"]

    post = post_store.get(post_id)
    issues = validation.validate(post, post_store.images(post_id), [], check_budget=False)
    codes = {issue.code for issue in issues if issue.level == validation.ERROR}
    assert "file_missing" not in codes, "a missing file is not an error here"
    assert "post_image_limit" not in codes, "adopted remote images do not count against it"


def test_already_attached_images_are_not_uploaded_again(fake, ready_post):
    from backend import db as db_module

    run_push(ready_post)
    before = len(fake.uploads)

    # a second run: everything already hangs on the post
    with db_module.transaction() as conn:
        conn.execute("UPDATE post_images SET remote_uuid=NULL, uploaded_at=NULL")
    run_push(ready_post)

    assert len(fake.uploads) == before, "what already hangs on it is not uploaded again"


def test_the_run_log_can_be_narrowed_to_the_last_day(client):
    """The table is never pruned - a resume needs it - so the log narrows on the
    way out instead."""
    from backend import db
    from backend.store import runs as run_store

    recent = run_store.create_run("push", [])
    old = run_store.create_run("push", [])
    with db.transaction() as conn:
        conn.execute("UPDATE push_runs SET created_at='2020-01-01T00:00:00Z' WHERE id=?", (old,))

    everything = {row["id"] for row in client.get("/api/push/runs").json()["items"]}
    assert {recent, old} <= everything

    day = {row["id"] for row in client.get("/api/push/runs", params={"hours": 24}).json()["items"]}
    assert recent in day and old not in day


def test_a_failure_while_attaching_does_not_re_send_the_images(fake, ready_post, scanned):
    """Fresh staged UUIDs survive the failed addImage call and are reused."""
    fake.fail_at = "add_image"
    run_push(ready_post)

    post = post_store.get(ready_post)
    assert post["remote_post_id"] is not None, "the post was created"
    uploaded = len(fake.uploads)
    assert uploaded == len(scanned)

    fake.fail_at = None
    run_push(ready_post)

    assert len(fake.uploads) == uploaded, "the same bytes are not sent twice"


def test_a_staged_upload_is_not_reused_for_different_metadata(fake, ready_post, scanned):
    """The other half of the skip: it must not outlive the bytes it was staged for.

    The realistic shape is two posts sharing one image. The first stages it; the
    second carries an edit, so the same picture is different bytes and the staged
    upload must not be handed over.
    """
    from backend.posts import materialise
    from backend.store import edits, uploads

    fake.fail_at = "add_image"
    run_push(ready_post)
    assert len(fake.uploads) == len(scanned), "staged, but not attached yet"

    image = post_store.images(ready_post)[0]
    plain_text = materialise.upload_infotext(image)
    plain_key = materialise.upload_key(image, plain_text)
    assert uploads.fresh_uuid(plain_key), "an untouched image finds its staged upload"

    edits.save(
        scanned[0],
        draft={"prompt": "a different prompt", "negative_prompt": None, "fields": {}},
        touched=["prompt"],
        deleted=[],
    )
    edited_text = materialise.upload_infotext(image)
    edited_key = materialise.upload_key(image, edited_text)

    assert edited_key != plain_key, "the edit is part of the identity"
    assert edited_key.startswith(f"{image['sha256']}:"), "and the original hash still leads it"
    assert uploads.fresh_uuid(edited_key) is None, "so the old upload is not offered"
    assert not uploads.staged_for(image["remote_uuid"], edited_key), (
        "and this post's own staged uuid no longer matches either"
    )


def test_attaching_a_model_changes_the_upload_key(fake, client, ready_post, scanned):
    from backend.posts import materialise
    from backend.store import images as image_store
    from backend.store import uploads

    fake.fail_at = "add_image"
    run_push(ready_post)

    image = post_store.images(ready_post)[0]
    before_text = materialise.upload_infotext(image)
    before_key = materialise.upload_key(image, before_text)
    assert uploads.fresh_uuid(before_key)
    resource = next(
        row for row in image_store.resources_for(scanned[0]) if row["model_version_id"]
    )

    attached = client.post(
        f"/api/resources/{resource['id']}/attach",
        json={
            "model": {"id": 901, "name": "Replacement model"},
            "version": {"id": 902, "name": "Replacement version", "hash_autov2": "ABCDEF1234"},
        },
    )
    assert attached.status_code == 200

    image = post_store.images(ready_post)[0]
    after_text = materialise.upload_infotext(image)
    after_key = materialise.upload_key(image, after_text)
    assert after_key != before_key
    assert uploads.fresh_uuid(after_key) is None
    assert not uploads.staged_for(image["remote_uuid"], after_key)


def test_an_identical_resource_lock_keeps_the_same_projected_upload_variant(
    client, ready_post, scanned, tmp_path
):
    from backend.posts import materialise
    from backend.store import images as image_store

    image_id = scanned[0]
    image = next(
        row for row in post_store.images(ready_post) if row["image_id"] == image_id
    )
    before_text = materialise.upload_infotext(image)
    before_key = materialise.upload_key(image, before_text)
    resource = next(
        row for row in image_store.resources_for(image_id) if row["model_version_id"]
    )
    image_store.update_resource(resource["id"], {"locked_by_user": 1})

    after_text = materialise.upload_infotext(image)
    after_key = materialise.upload_key(image, after_text)
    upload_directory = tmp_path / "uploads"
    path = materialise.path_for(image, upload_directory, after_text)

    assert after_text == before_text
    assert after_key == before_key
    assert after_text != image["raw_infotext"]
    assert after_key.startswith(f"{image['sha256']}:")
    assert path != Path(image["source_path"])
    assert path.exists()


def test_pressing_push_again_after_a_lost_create_does_not_create_a_second_post(
    fake, ready_post, client
):
    """The most expensive mistake this app could make, through the API rather
    than by hand-building the cursor.

    A retry starts a *new* run whose cursor is empty, so the row that records
    "a create was attempted" belongs to the previous run. Consulting only the
    current one made the safeguard unreachable from the UI - and the second
    press created a second post on a public account.
    """
    def push_through_the_api() -> None:
        """POST /api/push starts a background job; wait it out."""
        import time

        from backend import jobs

        response = client.post("/api/push", json={"post_ids": [ready_post]})
        job_id = response.json()["id"]
        for _ in range(200):
            job = jobs.get(job_id)
            if job and job.status not in ("starting", "running"):
                return
            time.sleep(0.02)
        raise AssertionError("the push job never finished")

    fake.fail_at = "create"
    push_through_the_api()
    assert fake.created == [], "the create never landed"
    assert post_store.get(ready_post)["remote_post_id"] is None

    fake.fail_at = None
    push_through_the_api()

    assert fake.created == [], "and the retry looks for it instead of making another"
    assert post_store.get(ready_post)["state"] == lifecycle.NEEDS_RECONCILE


def test_reordering_a_post_keeps_a_video_marked_as_one(fake, ready_post, scanned):
    """media_type is learned from CivitAI, not derived from the image row, so
    nothing puts it back. Losing it made the next fetch try to download a video
    this app deliberately leaves alone."""
    from backend import db

    rows = post_store.images(ready_post)
    with db.transaction() as conn:
        conn.execute("UPDATE post_images SET media_type='video' WHERE id=?", (rows[0]["id"],))

    post_store.set_images(ready_post, [scanned[1], scanned[0]])

    kinds = {row["image_id"]: row["media_type"] for row in post_store.images(ready_post)}
    assert kinds[scanned[0]] == "video", "still a video after the reorder"
    assert kinds[scanned[1]] == "image"


def test_an_expired_absolute_time_stays_a_draft_and_the_batch_carries_on(
    fake, client, scanned, monkeypatch
):
    """The absolute plan was legal at preflight but expired during its upload."""
    from datetime import timedelta

    from backend.posts import schedule

    real_now = schedule.utcnow()
    when = real_now + timedelta(minutes=70)
    expiring = client.post(
        "/api/posts",
        json={
            "title": "Expiring absolute time",
            "image_ids": [scanned[0]],
            "schedule_mode": "absolute",
            "scheduled_at": schedule.iso_z(when),
        },
    ).json()["id"]
    sibling = client.post(
        "/api/posts",
        json={
            "title": "Relative sibling",
            "image_ids": [scanned[1]],
            "schedule_mode": "relative",
            "schedule_offset_minutes": 120,
        },
    ).json()["id"]

    clock = {"offset": timedelta(0)}
    monkeypatch.setattr(schedule, "utcnow", lambda: real_now + clock["offset"])

    def slow_upload(path, content_type=None):
        clock["offset"] = timedelta(minutes=20)
        return fake.upload_image(path, content_type)

    monkeypatch.setattr(push.mcp, "upload_image", slow_upload)
    job = jobs.Job(id=0, kind="push")
    push.push_posts(job, [expiring, sibling])

    stopped = post_store.get(expiring)
    assert stopped["state"] == lifecycle.REMOTE_DRAFT
    assert stopped["remote_published_at"] is None
    assert all(image["remote_image_id"] for image in post_store.images(expiring))
    assert "60 minutes" in stopped["last_error"]
    assert "still a draft" in stopped["last_error"]
    assert fake.posts[stopped["remote_post_id"]]["publishedAt"] is None

    assert job.failed == 1
    assert job.succeeded == 1
    assert post_store.get(sibling)["state"] == lifecycle.SCHEDULED
    assert len(fake.updates) == 1, "only the sibling receives a publish time"


def test_startup_recovery_keeps_a_created_post_as_a_draft(fake, ready_post):
    fake.fail_at = "add_image"
    run_push(ready_post)

    with db.transaction() as conn:
        conn.execute("UPDATE posts SET state='pushing' WHERE id=?", (ready_post,))
        conn.execute("UPDATE push_runs SET status='running'")

    recovered = run_store.recover_interrupted()
    assert recovered["remote_draft"] == 1
    assert recovered["failed"] == 0
    post = post_store.get(ready_post)
    assert post["state"] == lifecycle.REMOTE_DRAFT
    assert "2 of 2 images are missing" in post["last_error"]
    assert "still a draft" in post["last_error"]


def test_startup_recovery_ignores_an_attempt_from_an_older_run(fake, ready_post):
    """The unresolved-attempt lookup is scoped to the runs that were just
    interrupted: one from a run that finished long ago says nothing about this
    push, and would strand the post in needs_reconcile for no reason."""
    fake.fail_at = "create"
    run_push(ready_post)

    with db.transaction() as conn:
        # the old run is closed, and the post starts a fresh push that dies
        # before create_post is even attempted
        conn.execute("UPDATE push_runs SET status='error'")
        conn.execute("UPDATE posts SET state='pushing' WHERE id=?", (ready_post,))
        run_id = conn.execute(
            "INSERT INTO push_runs(kind, status, total, created_at) VALUES('push','running',1,?)",
            (db.now_iso(),),
        ).lastrowid
        conn.execute(
            "INSERT INTO push_items(run_id, post_id, status, step, updated_at)"
            " VALUES(?,?,'running','queued',?)",
            (run_id, ready_post, db.now_iso()),
        )

    recovered = run_store.recover_interrupted()
    assert recovered["needs_reconcile"] == 0
    assert recovered["failed"] == 1
    assert post_store.get(ready_post)["state"] == lifecycle.FAILED


def _emitted_issue_codes() -> set[str]:
    """Every code the checklist can carry, read out of the source.

    Read rather than listed: a list would go stale the moment someone adds an
    issue, which is the failure this guard exists to catch. ``Issue`` and
    ``ScheduleCheck`` name the code in their second argument, ``Hold`` in its
    first; the pass-through constructions (``check.code``, ``hold.code``) are
    not literals and are covered by the modules they come from.
    """
    import ast

    root = Path(__file__).resolve().parents[2] / "backend"
    positions = {"Issue": 1, "ScheduleCheck": 1, "Hold": 0}
    codes: set[str] = set()
    for module in ("posts/validation.py", "posts/schedule.py", "civitai/ratelimit.py"):
        tree = ast.parse((root / module).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            index = positions.get(node.func.id)
            if index is None or len(node.args) <= index:
                continue
            argument = node.args[index]
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                codes.add(argument.value)
    return codes


def test_every_issue_code_the_checklist_can_emit_is_in_the_catalogue():
    codes = _emitted_issue_codes()
    assert "binding_locked" in codes and "too_soon" in codes, "the reader found nothing"

    catalogue = json.loads(
        (Path(__file__).resolve().parents[2] / "frontend/src/i18n/en.json").read_text(
            encoding="utf-8"
        )
    )
    missing = sorted(code for code in codes if f"issue.{code}" not in catalogue)
    assert not missing, f"no English catalogue entry: {missing}"


def test_every_issue_string_is_actually_translated():
    root = Path(__file__).resolve().parents[2] / "frontend/src/i18n"
    english = json.loads((root / "en.json").read_text(encoding="utf-8"))
    german = json.loads((root / "de.json").read_text(encoding="utf-8"))

    keys = [key for key in english if key.startswith("issue.")]
    assert keys, "no issue entries at all"
    assert not [key for key in keys if key not in german], "missing from de.json"
    untranslated = [key for key in keys if german[key] == english[key]]
    assert not untranslated, f"still the English sentence: {untranslated}"


def test_the_daily_limit_stops_the_run_and_leaves_the_rest_alone(
    fake, scanned, client, monkeypatch
):
    """A rate limit aborts the batch; it does not fail post after post.

    The exemption in `_preflight` is keyed on the codes `ratelimit.check` can
    return. When that split into two codes the string it compared against went
    stale, every post hit `RuntimeError` instead of `PushAborted` and was marked
    `failed` in turn - so the check is now on the named set, and this is the
    test that would have caught it.
    """
    from backend.civitai import ratelimit

    first = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    second = client.post("/api/posts", json={"image_ids": [scanned[1]]}).json()["id"]
    for post_id in (first, second):
        client.patch(f"/api/posts/{post_id}", json={"title": "T", "schedule_offset_minutes": 90})
        client.post(f"/api/posts/{post_id}/ready")

    hold = ratelimit.Hold(
        "rate_limit_daily",
        "Daily limit reached: 5 of 5 posts in the last 24 hours.",
        {"used": 5, "limit": 5},
    )
    monkeypatch.setattr(push.ratelimit, "check", lambda pending=1: hold)

    job = jobs.Job(id=0, kind="push")
    push.push_posts(job, [first, second])

    assert job.failed == 0, "a rate limit is not a per-post failure"
    assert job.skipped == 1, "it stopped at the first post"
    assert job.error, "and the run says why"
    assert post_store.get(first)["state"] != lifecycle.FAILED
    assert post_store.get(second)["state"] == lifecycle.READY, "the rest is untouched"
