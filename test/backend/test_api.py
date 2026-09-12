"""The HTTP surface, end to end through the real app."""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
import traceback
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image

from backend import db


def _diagnostic_events() -> list[dict]:
    from backend import config

    events = []
    for path in config.data_dir().glob(f"{config.DIAGNOSTIC_LOG_FILENAME}*"):
        for line in path.read_text(encoding="utf-8").splitlines():
            _, marker, payload = line.partition(" ERROR ")
            if not marker:
                continue
            events.append(json.loads(payload))
    return events


@pytest.fixture(autouse=True)
def reconstructed_hash_lookup(monkeypatch):
    """Keep API tests local while preserving the two verified REST call shapes."""
    from backend.civitai import rest

    monkeypatch.setattr(rest.client, "get_json", lambda *args, **kwargs: None)
    monkeypatch.setattr(rest.client, "post_json", lambda *args, **kwargs: [])


def test_health(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_every_single_post_mutation_refuses_a_pushing_post(client):
    from backend.api import posts as posts_api
    from backend.posts import lifecycle
    from backend.store import posts as post_store

    guarded_routes = {
        ("PATCH", "/api/posts/{post_id}"): {},
        ("DELETE", "/api/posts/{post_id}"): None,
        ("PUT", "/api/posts/{post_id}/images"): {"image_ids": []},
        ("PUT", "/api/posts/{post_id}/image-order"): {"post_image_ids": []},
        ("PUT", "/api/posts/{post_id}/tags"): {"tags": []},
        ("POST", "/api/posts/{post_id}/images/{post_image_id}/dedup-ack"): None,
        ("POST", "/api/posts/{post_id}/images/{post_image_id}/hide-meta"): None,
        ("POST", "/api/posts/{post_id}/ready"): None,
        ("POST", "/api/posts/{post_id}/unready"): None,
        ("POST", "/api/posts/{post_id}/archive"): None,
        ("POST", "/api/posts/{post_id}/archive-folder/rename"): None,
        ("POST", "/api/posts/{post_id}/reschedule"): {},
        ("POST", "/api/posts/{post_id}/push-text"): None,
        ("POST", "/api/posts/{post_id}/remote-delete"): None,
        ("POST", "/api/posts/{post_id}/rebuild"): None,
        ("POST", "/api/posts/{post_id}/adopt-remote-snapshot"): None,
        ("POST", "/api/posts/{post_id}/reconcile"): {"remote_post_id": 1},
        ("POST", "/api/posts/{post_id}/match-local"): None,
        (
            "POST",
            "/api/posts/{post_id}/images/{post_image_id}/match-local/{image_id}",
        ): None,
        ("POST", "/api/posts/{post_id}/fetch-images"): None,
        ("POST", "/api/posts/{post_id}/sync"): None,
    }
    exemptions = {
        ("POST", "/api/posts"): "creates a new post, so there is no owned post to guard",
        (
            "POST",
            "/api/posts/archive-published",
        ): "batch route; its published-only query cannot select a pushing post",
        (
            "POST",
            "/api/posts/archive-selected",
        ): "batch route; its existing transition check refuses pushing -> archived",
        (
            "POST",
            "/api/posts/match-local",
        ): "batch route; its candidate query excludes a pushing post",
    }
    mutating_methods = {"POST", "PUT", "PATCH", "DELETE"}
    registered_routes = {
        (method, route.path)
        for route in posts_api.router.routes
        for method in route.methods & mutating_methods
    }
    assert registered_routes == guarded_routes.keys() | exemptions.keys()

    post_id = post_store.create(title="Owned by a push")
    post_store.set_fields(post_id, state=lifecycle.PUSHING)
    for (method, route), payload in guarded_routes.items():
        path = route.format(post_id=post_id, post_image_id=1, image_id=1)
        request_args = {} if payload is None else {"json": payload}
        response = client.request(method, path, **request_args)

        assert response.status_code == 409, (method, route, response.text)
        assert response.json()["detail"]["code"] == "post_push_in_progress"
        assert post_store.get(post_id)["state"] == lifecycle.PUSHING


def test_mark_ready_does_not_take_a_pushing_post_from_its_run(client):
    from backend.posts import lifecycle
    from backend.store import posts as post_store

    post_id = post_store.create(title="Owned by a push")
    post_store.set_fields(post_id, state=lifecycle.PUSHING)

    response = client.post(f"/api/posts/{post_id}/ready")

    assert lifecycle.can_transition(lifecycle.PUSHING, lifecycle.READY)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "post_push_in_progress"
    assert post_store.get(post_id)["state"] == lifecycle.PUSHING


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("https://civitai.com/models/456/example?modelVersionId=789", ("version", 789)),
        ("1234567890", ("id", 1234567890)),
        ("67ab2fd8ec", ("hash", "67ab2fd8ec")),
        ("Pony Diffusion", ("name", "Pony Diffusion")),
    ],
)
def test_model_search_lookup_recognises_each_supported_query(query, expected):
    from backend.api import resources

    assert resources.model_search_lookup(query) == expected


def test_model_search_by_hash_shapes_the_version_payload(client, monkeypatch):
    from backend.civitai import rest

    file_hash = "67ab2fd8ec"
    monkeypatch.setattr(
        rest,
        "model_version_by_hash",
        lambda value: {
            "id": 290640,
            "modelId": 257749,
            "name": "V6",
            "model": {"name": "Pony Diffusion V6 XL", "type": "Checkpoint", "nsfw": False},
            "files": [{"hashes": {"AutoV2": value, "SHA256": "a" * 64}}],
            "images": [{"url": "https://example.invalid/thumbnail.jpg"}],
        },
    )

    response = client.get("/api/models/search", params={"q": file_hash})

    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["name"] == "Pony Diffusion V6 XL"
    assert item["type"] == "Checkpoint"
    assert item["versions"][0]["id"] == 290640


def test_unknown_model_version_id_falls_back_to_name_search(client, monkeypatch):
    from backend.civitai import rest

    calls = []
    result = {
        "id": 2,
        "name": "1234567890",
        "type": "LoRA",
        "nsfw": False,
        "creator": None,
        "download_count": None,
        "versions": [],
        "thumbnail_url": None,
    }
    monkeypatch.setattr(rest, "model_version_by_id", lambda value: calls.append(("version", value)))
    monkeypatch.setattr(rest, "model_by_id", lambda value: calls.append(("model", value)))
    monkeypatch.setattr(
        rest,
        "search_models",
        lambda query, **kwargs: calls.append(("name", query)) or [result],
    )

    response = client.get("/api/models/search", params={"q": "1234567890"})

    assert response.status_code == 200
    assert response.json()["items"] == [result]
    assert calls == [("version", 1234567890), ("model", 1234567890), ("name", "1234567890")]


def test_model_url_without_version_resolves_its_model(client, monkeypatch):
    from backend.civitai import rest

    result = {
        "id": 456,
        "name": "Pony Diffusion V6 XL",
        "type": "Checkpoint",
        "nsfw": False,
        "creator": None,
        "download_count": None,
        "versions": [],
        "thumbnail_url": None,
    }
    calls = []
    monkeypatch.setattr(rest, "model_by_id", lambda value: calls.append(value) or result)

    response = client.get("/api/models/search", params={"q": "https://civitai.com/models/456"})

    assert response.status_code == 200
    assert response.json()["items"] == [result]
    assert calls == [456]


def test_model_id_lookups_make_one_distinct_rest_request_each(monkeypatch):
    from backend.civitai import rest

    calls = []

    def get_json(url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/models/12"):
            return {"id": 12, "name": "Model", "modelVersions": []}
        return {
            "id": 34,
            "modelId": 12,
            "name": "Version",
            "model": {"name": "Model", "type": "LoRA", "nsfw": False},
        }

    monkeypatch.setattr(rest.client, "get_json", get_json)

    assert rest.model_by_id(12)["id"] == 12
    assert rest.model_version_by_id(34)["versions"][0]["id"] == 34
    assert [call[0] for call in calls] == [
        f"{rest.config.rest_base()}/models/12",
        f"{rest.config.rest_base()}/model-versions/34",
    ]
    assert [call[1]["procedure"] for call in calls] == ["models/{id}", "model-versions/{id}"]


def test_shell_serves_the_built_frontend_with_assets(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "/assets/" in response.text


def test_the_api_key_settings_surface_is_gone(client):
    response = client.put(
        "/api/settings",
        json={"civitai_api_token": "ignored", "clear_token": True},
    )

    assert response.status_code == 200
    assert db.get_setting("civitai_api_token") is None
    assert client.post("/api/settings/civitai-token/test").status_code == 405


def test_serve_prints_the_active_diagnostic_path(monkeypatch, capsys):
    import uvicorn

    from backend import cli, config

    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: None)
    result = cli._serve(
        Namespace(host="127.0.0.1", port=8430, no_browser=True, reload=False)
    )

    assert result == 0
    assert f"Diagnostics: {config.diagnostic_log_path().resolve()}" in capsys.readouterr().out


def test_validation_errors_name_the_request_and_complete_location(client):
    db.set_setting("debug_logging", "1")
    cases = (
        (
            client.post("/api/posts", json={"model_version_id": "body-not-an-integer"}),
            "POST",
            "/api/posts",
            "body.model_version_id",
        ),
        (
            client.get("/api/images?limit=query-not-an-integer"),
            "GET",
            "/api/images",
            "query.limit",
        ),
        (
            client.get("/api/posts/path-not-an-integer"),
            "GET",
            "/api/posts/path-not-an-integer",
            "path.post_id",
        ),
    )

    logged = {event["diagnostic_id"]: event for event in _diagnostic_events()}
    for response, method, endpoint, location in cases:
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert detail["code"] == "request_validation_error"
        assert detail["params"]["method"] == method
        assert detail["params"]["endpoint"] == endpoint
        assert detail["errors"][0] == {
            "type": "int_parsing",
            "location": location,
            "message": "Input should be a valid integer, unable to parse string as an integer",
            "input_type": "str",
        }

        event = logged[detail["params"]["diagnostic_id"]]
        assert event["method"] == method
        assert event["path"] == endpoint
        assert event["errors"] == detail["errors"]


def test_multiple_and_simultaneous_validation_errors_stay_separate(client):
    db.set_setting("debug_logging", "1")
    multiple = client.post(
        "/api/posts",
        json={"model_version_id": "bad-model", "collection_id": "bad-collection"},
    )
    assert [issue["location"] for issue in multiple.json()["detail"]["errors"]] == [
        "body.model_version_id",
        "body.collection_id",
    ]

    barrier = threading.Barrier(2)

    def fail(value: str):
        barrier.wait()
        return client.get("/api/images", params={"limit": value})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(fail, ("bad-one", "bad-two")))

    ids = [response.json()["detail"]["params"]["diagnostic_id"] for response in responses]
    assert len(set(ids)) == 2
    logged = {event["diagnostic_id"]: event for event in _diagnostic_events()}
    assert all(
        logged[diagnostic_id]["errors"][0]["location"] == "query.limit"
        for diagnostic_id in ids
    )


def test_unhandled_errors_are_safe_and_expected_http_errors_are_not_logged(
    monkeypatch,
):
    from fastapi.testclient import TestClient

    from backend import config
    from backend.main import create_app
    from backend.store import posts as post_store

    private_message = "exception-private-value"
    db.set_setting("debug_logging", "1")

    def crash(*args, **kwargs):
        raise RuntimeError(private_message)

    monkeypatch.setattr(post_store, "list_posts", crash)
    with TestClient(create_app(), raise_server_exceptions=False) as test_client:
        response = test_client.get("/api/posts")
        not_found = test_client.get("/api/posts/999999")

    assert response.status_code == 500
    detail = response.json()["detail"]
    assert detail["code"] == "unexpected_error"
    diagnostic_id = detail["params"]["diagnostic_id"]
    assert private_message not in response.text
    assert not_found.status_code == 404
    assert not_found.json()["detail"]["code"] == "post_not_found"

    events = _diagnostic_events()
    unexpected = [event for event in events if event["event"] == "unexpected_error"]
    assert [event["diagnostic_id"] for event in unexpected] == [diagnostic_id]
    assert unexpected[0]["exception_type"] == "RuntimeError"
    frames = unexpected[0]["frames"]
    assert frames[-1] == {
        "module": "test/backend/test_api.py",
        "function": "crash",
        "line": crash.__code__.co_firstlineno + 1,
    }

    retained = "\n".join(
        path.read_text(encoding="utf-8")
        for path in config.data_dir().glob(f"{config.DIAGNOSTIC_LOG_FILENAME}*")
    )
    assert "Traceback (most recent call last)" not in retained
    assert '"module":"test/backend/test_api.py"' in retained
    assert "raise RuntimeError(private_message)" not in retained
    assert str(Path.home()) not in retained
    assert re.search(r"[A-Za-z]:[\\/]", retained) is None
    assert private_message not in retained


def test_diagnostic_frames_remove_machine_paths():
    from backend import diagnostics

    frames = [
        traceback.FrameSummary(
            "/home/reporter/livebound/backend/posts/push.py", 812, "push", line="secret"
        ),
        traceback.FrameSummary(
            r"C:\\Users\\reporter\\livebound\\.venv\\Lib\\site-packages\\httpx\\_client.py",
            101,
            "send",
            line="secret",
        ),
    ]

    assert [diagnostics._frame_module(frame.filename) for frame in frames] == [
        "backend/posts/push.py",
        "site-packages/httpx/_client.py",
    ]


def test_diagnostic_setup_and_write_failures_do_not_replace_safe_responses(monkeypatch):
    from fastapi.testclient import TestClient

    from backend import diagnostics
    from backend.main import create_app
    from backend.store import posts as post_store

    def logging_failed(*args, **kwargs):
        raise OSError("diagnostic storage unavailable")

    monkeypatch.setattr(diagnostics, "_active_path", None)
    monkeypatch.setattr(diagnostics, "RotatingFileHandler", logging_failed)
    app = create_app()
    monkeypatch.setattr(diagnostics._logger, "error", logging_failed)

    private_message = "must-stay-private"

    def crash(*args, **kwargs):
        raise RuntimeError(private_message)

    monkeypatch.setattr(post_store, "list_posts", crash)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        invalid = test_client.get("/api/images", params={"limit": "not-an-integer"})
        unexpected = test_client.get("/api/posts")

    assert invalid.status_code == 422
    assert invalid.json()["detail"]["code"] == "request_validation_error"
    assert unexpected.status_code == 500
    # The write failed, so there is no entry - and the response says that
    # instead of sending somebody to look up an id that was never written.
    assert unexpected.json()["detail"]["code"] == "unexpected_error_no_diagnostic"
    assert "diagnostic" in unexpected.json()["detail"]["message"]
    assert private_message not in unexpected.text


def test_setup_settings_report_fresh_and_configured_installations(client, tmp_path):
    fresh = client.get("/api/settings").json()
    assert fresh["civitai_auth_mode"] == "none"
    assert fresh["setup"] == {
        "source_folder": False,
        "archive_folder": False,
        "adopted_folder": False,
        "model_root": False,
    }

    folders = {
        name: tmp_path / name for name in ("images", "archive", "adopted", "models")
    }
    for folder in folders.values():
        folder.mkdir()
    client.post("/api/sources", json={"path": str(folders["images"])})
    client.post(
        "/api/sources",
        json={"path": str(folders["archive"]), "is_archive": True},
    )
    client.post("/api/model-roots", json={"path": str(folders["models"])})
    configured = client.put(
        "/api/settings",
        json={
            "adopted_folder": str(folders["adopted"]),
        },
    ).json()

    assert configured["civitai_auth_mode"] == "none"
    assert all(configured["setup"].values())


def test_settings_report_project_identity_and_serve_its_licence(client):
    from backend import config

    settings = client.get("/api/settings").json()
    assert settings["app_licence"] == config.APP_LICENCE
    assert settings["app_rights_holder"] == config.APP_RIGHTS_HOLDER

    licence = client.get("/LICENSE")
    assert licence.status_code == 200
    assert licence.headers["content-type"].startswith("text/plain")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in licence.text


def test_adding_a_missing_folder_is_refused(client):
    response = client.post("/api/sources", json={"path": "/does/not/exist"})
    assert response.status_code == 400


def test_scan_indexes_the_folder(client, scanned):
    assert len(scanned) >= 1
    images = client.get("/api/images").json()
    assert images["total"] == len(scanned)
    assert all(image["sha256"] for image in images["items"])


def test_library_collapses_exact_pixels_before_paging(client, scanned, monkeypatch):
    from backend import duplicates, jobs
    from backend.scanner import service
    from backend.store import images as image_store
    from backend.store import usage

    original = Path(image_store.get(scanned[0])["absolute_path"])
    exact_copy = original.with_name(f"{original.stem}-copy{original.suffix}")
    other_container = original.with_name(f"{original.stem}-copy.webp")
    shutil.copy2(original, exact_copy)
    with Image.open(original) as image:
        image.convert("RGB").save(other_container, "WEBP", lossless=True)
    service.scan_roots(jobs.Job(id=0, kind="scan"))

    page = client.get("/api/images", params={"limit": len(scanned)}).json()
    duplicate = next(item for item in page["items"] if item["duplicate_count"] == 3)

    assert page["total"] == len(scanned)
    assert len(page["items"]) == len(scanned)
    assert len(duplicate["duplicate_members"]) == 3

    library_group_calls = []
    original_library_groups = duplicates.library_groups

    def observed_library_groups(image_ids=None):
        library_group_calls.append(image_ids)
        return original_library_groups(image_ids)

    monkeypatch.setattr(duplicates, "library_groups", observed_library_groups)
    filtered = client.get("/api/images", params={"q": other_container.name}).json()
    assert filtered["total"] == 1
    assert len(filtered["items"]) == 1
    assert filtered["items"][0]["duplicate_count"] == 1
    assert filtered["items"][0]["duplicate_members"] == []
    assert library_group_calls == [[image_store.get_by_path(str(other_container))["id"]]]

    original_row = image_store.get_by_path(str(original))
    usage.record(
        sha256=original_row["sha256"],
        pixel_sha256=original_row["pixel_sha256"],
        phash=original_row["phash"],
        post_id=None,
    )
    all_images = client.get("/api/images", params={"usage_state": "all"}).json()
    used = client.get("/api/images", params={"usage_state": "used"}).json()
    unused = client.get("/api/images", params={"usage_state": "unused"}).json()
    default = client.get("/api/images").json()

    used_stack = used["items"][0]
    assert used["usage_state"] == "used"
    assert used["total"] == 1
    assert used_stack["used"] is True
    assert used_stack["duplicate_count"] == 3
    assert {member["absolute_path"] for member in used_stack["duplicate_members"]} == {
        str(original),
        str(exact_copy),
        str(other_container),
    }
    assert all(item["used"] is False for item in unused["items"])

    def physical_ids(page):
        return {
            member["id"]
            for item in page["items"]
            for member in (item["duplicate_members"] or [item])
        }

    all_ids = physical_ids(all_images)
    used_ids = physical_ids(used)
    unused_ids = physical_ids(unused)
    assert default["usage_state"] == "unused"
    assert physical_ids(default) == unused_ids
    assert used_ids.isdisjoint(unused_ids)
    assert used_ids | unused_ids == all_ids


def test_folder_filter_includes_only_its_descendants(client, tmp_path):
    from backend import jobs
    from backend.scanner import service

    source = tmp_path / "folder-filter"

    def write_pattern(relative_path: str, difference_hash: str) -> Path:
        path = source / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        bits = f"{int(difference_hash, 16):064b}"
        pixels = []
        for row in range(8):
            values = [128]
            for bit in bits[row * 8 : (row + 1) * 8]:
                values.append(values[-1] - 12 if bit == "1" else values[-1] + 12)
            pixels.extend(values)
        image = Image.new("L", (9, 8))
        image.putdata(pixels)
        image.save(path)
        return path

    write_pattern("series/direct.png", "0000000000000000")
    duplicate = write_pattern("series/duplicate-a.png", "5555555555555555")
    write_pattern("series/remix/nested.png", "ffffffffffffffff")
    write_pattern("series/remix/final/deep.png", "aaaaaaaaaaaaaaaa")
    write_pattern("series-old/sibling.png", "0f0f0f0f0f0f0f0f")
    shutil.copy2(duplicate, source / "series/remix/duplicate-b.png")
    shutil.copy2(duplicate, source / "series-old/duplicate-c.png")
    write_pattern("literal%_name/direct.png", "3333333333333333")
    write_pattern("literal%_name/child/nested.png", "cccccccccccccccc")
    write_pattern("literalXXname/wildcard-sibling.png", "00ff00ff00ff00ff")

    client.post("/api/sources", json={"path": str(source), "label": "Test"})
    service.scan_roots(jobs.Job(id=0, kind="scan"))

    page = client.get("/api/images", params={"folder": "series"}).json()

    disclosed_paths = {
        member["relative_path"]
        for item in page["items"]
        for member in (item["duplicate_members"] or [item])
    }
    assert page["total"] == 4
    assert disclosed_paths == {
        "series/direct.png",
        "series/duplicate-a.png",
        "series/remix/duplicate-b.png",
        "series/remix/nested.png",
        "series/remix/final/deep.png",
    }
    stack = next(item for item in page["items"] if item["duplicate_count"] == 2)
    assert {member["folder"] for member in stack["duplicate_members"]} == {
        "series",
        "series/remix",
    }

    literal = client.get(
        "/api/images", params={"folder": "literal%_name"}
    ).json()
    assert literal["total"] == 2
    assert {item["relative_path"] for item in literal["items"]} == {
        "literal%_name/direct.png",
        "literal%_name/child/nested.png",
    }


def test_image_detail_carries_the_readable_metadata(client, scanned):
    detail = client.get(f"/api/images/{scanned[0]}").json()
    assert detail["metadata"]["has_metadata"]
    assert detail["metadata"]["prompt"]
    assert "duplicates" in detail


def test_thumbnail_and_preview_are_served(client, scanned):
    for suffix in ("thumbnail", "preview"):
        response = client.get(f"/api/images/{scanned[0]}/{suffix}")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/")


def test_a_post_with_a_time_becomes_ready_by_itself(client, scanned):
    post = client.post(
        "/api/posts",
        json={"title": "T", "image_ids": scanned, "schedule_offset_minutes": 120},
    ).json()
    assert post["state"] == "ready"
    assert post["resolved_publish_at"]
    assert post["offset_label"] == "in 2 h"


def test_a_post_without_a_time_stays_a_draft(client, scanned):
    post = client.post("/api/posts", json={"image_ids": scanned}).json()
    assert post["state"] == "draft"
    assert post["checklist"]["blocked"] is True


def test_a_refused_transition_returns_a_translatable_conflict(client):
    from backend.posts import lifecycle
    from backend.store import posts as post_store

    post = client.post("/api/posts", json={}).json()
    post_store.set_fields(post["id"], state=lifecycle.REMOTE_DRAFT)

    response = client.post(f"/api/posts/{post['id']}/archive")

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": lifecycle.ERROR_NOT_ALLOWED,
        "message": "The step draft on CivitAI -> archived is not allowed.",
        "params": {"current": lifecycle.REMOTE_DRAFT, "target": lifecycle.ARCHIVED},
    }


def test_image_order_is_preserved(client, scanned):
    if len(scanned) < 2:
        return
    post = client.post("/api/posts", json={"image_ids": scanned}).json()
    reversed_ids = list(reversed(scanned))
    updated = client.put(f"/api/posts/{post['id']}/images", json={"image_ids": reversed_ids}).json()
    assert [image["image_id"] for image in updated["images"]] == reversed_ids


def _image_selection(client, scanned, tmp_path, count):
    """Create enough real local rows to exercise a post-sized selection."""
    from backend.store import images as image_store

    image_ids = list(scanned)
    source_root_id = image_store.get(scanned[0])["source_root_id"]
    for index in range(len(image_ids), count):
        path = tmp_path / f"post-image-limit-{index}.png"
        path.touch()
        image_ids.append(
            image_store.upsert(
                source_root_id,
                {
                    "absolute_path": str(path),
                    "relative_path": path.name,
                    "file_size": 0,
                    "content_type": "image/png",
                    "width": 1,
                    "height": 1,
                },
            )
        )
    return image_ids


def test_post_image_limit_is_enforced_by_the_store_and_route(client, scanned, tmp_path):
    from backend import config
    from backend.store import posts as post_store

    image_ids = _image_selection(client, scanned, tmp_path, config.CIVITAI_POST_IMAGE_LIMIT + 1)
    post_id = post_store.create()

    post_store.set_images(post_id, image_ids[: config.CIVITAI_POST_IMAGE_LIMIT])
    assert len(post_store.images(post_id)) == config.CIVITAI_POST_IMAGE_LIMIT

    with pytest.raises(post_store.PostImageLimitExceeded) as exc_info:
        post_store.set_images(post_id, image_ids)
    assert exc_info.value.count == config.CIVITAI_POST_IMAGE_LIMIT + 1
    assert exc_info.value.limit == config.CIVITAI_POST_IMAGE_LIMIT

    accepted = client.post(
        "/api/posts", json={"image_ids": image_ids[: config.CIVITAI_POST_IMAGE_LIMIT]}
    )
    assert accepted.status_code == 200

    refused = client.put(
        f"/api/posts/{accepted.json()['id']}/images", json={"image_ids": image_ids}
    )
    assert refused.status_code == 400
    assert refused.json()["detail"] == {
        "code": "post_image_limit",
        "message": "A post can have at most 20 images; 21 were selected. Remove 1 image(s).",
        "params": {"limit": 20, "count": 21, "excess": 1},
    }


def test_preflight_reports_an_older_post_over_the_image_limit(client, scanned, tmp_path):
    from backend import config
    from backend.posts import validation
    from backend.store import images as image_store
    from backend.store import posts as post_store

    image_ids = _image_selection(client, scanned, tmp_path, config.CIVITAI_POST_IMAGE_LIMIT + 1)
    post_id = post_store.create()
    records = image_store.get_many(image_ids)
    with db.transaction() as conn:
        conn.executemany(
            """
            INSERT INTO post_images(post_id, image_id, position, source_path, sha256)
            VALUES(?,?,?,?,?)
            """,
            [
                (post_id, record["id"], position, record["absolute_path"], record["sha256"] or "")
                for position, record in enumerate(records)
            ],
        )

    issues = validation.validate(
        post_store.get(post_id), post_store.images(post_id), [], check_budget=False
    )
    issue = next(issue for issue in issues if issue.code == "post_image_limit")
    assert issue.level == validation.ERROR
    assert issue.params == {"limit": 20, "count": 21, "excess": 1}


def test_adding_unknown_loras_uses_one_bulk_lookup_and_caches_the_answers(
    client, scanned, monkeypatch
):
    from backend.civitai import rest
    from backend.store import images as image_store

    known_hash = "A" * 64
    unknown_hash = "B" * 64
    image_store.replace_resources(
        scanned[0],
        [
            {
                "resource_type": "lora",
                "name_in_prompt": "KnownStyle",
                "hash": known_hash,
            },
            {
                "resource_type": "lora",
                "name_in_prompt": "UnknownStyle",
                "hash": unknown_hash,
            },
        ],
    )
    requests = []

    def reconstructed_civitai(_url, hashes, **_kwargs):
        requests.append(hashes)
        return [
            {
                "id": 4242,
                "modelId": 42,
                "name": "v1",
                "model": {"name": "Known style", "type": "LoRA"},
                "files": [{"hashes": {"SHA256": known_hash}}],
            }
        ]

    monkeypatch.setattr(rest.client, "post_json", reconstructed_civitai)

    body = {
        "title": "T",
        "tags": ["x"],
        "schedule_offset_minutes": 120,
    }
    post = client.post("/api/posts", json=body).json()
    first = client.put(
        f"/api/posts/{post['id']}/images", json={"image_ids": [scanned[0]]}
    ).json()
    client.put(f"/api/posts/{post['id']}/images", json={"image_ids": []})
    second = client.put(
        f"/api/posts/{post['id']}/images", json={"image_ids": [scanned[0]]}
    ).json()

    assert requests == [[known_hash, unknown_hash]]
    assert first["resource_resolution"] == {
        "resolved": 1,
        "unknown": 1,
        "cached": 0,
        "failed": 0,
        "reapplied": 1,
    }
    assert second["resource_resolution"] == {
        "resolved": 0,
        "unknown": 0,
        "cached": 1,
        "failed": 0,
        "reapplied": 0,
    }
    known_resource = next(
        row
        for row in image_store.resources_for(scanned[0])
        if row["name_in_prompt"] == "KnownStyle"
    )
    assert known_resource["model_version_id"] == 4242
    issue = next(
        item
        for item in second["checklist"]["issues"]
        if item["code"] == "unresolved_resources"
    )
    assert issue["level"] == "info"
    assert second["checklist"]["can_push"] is True


def test_a_failed_resource_lookup_does_not_block_adding_the_image(
    client, scanned, monkeypatch
):
    from backend.civitai import rest
    from backend.civitai.errors import CivitaiError
    from backend.store import images as image_store

    image_store.replace_resources(
        scanned[0],
        [
            {
                "resource_type": "lora",
                "name_in_prompt": "TemporarilyUnavailable",
                "hash": "C" * 64,
            }
        ],
    )

    def unavailable(*_args, **_kwargs):
        raise CivitaiError("temporary failure")

    monkeypatch.setattr(rest.client, "post_json", unavailable)
    post = client.post("/api/posts", json={}).json()
    response = client.put(
        f"/api/posts/{post['id']}/images", json={"image_ids": [scanned[0]]}
    )

    assert response.status_code == 200
    body = response.json()
    assert [image["image_id"] for image in body["images"]] == [scanned[0]]
    assert body["resource_resolution"]["failed"] == 1


def test_attaching_a_resource_assigns_the_hash_scope_with_its_names(client, scanned):
    from backend.store import images as image_store

    file_hash = "A" * 10
    first_image, second_image = scanned
    image_store.replace_resources(
        first_image,
        [{"resource_type": "lora", "name_in_prompt": "First", "hash": file_hash}],
    )
    image_store.replace_resources(
        second_image,
        [{"resource_type": "lora", "name_in_prompt": "Second", "hash": file_hash}],
    )
    first = image_store.resources_for(first_image)[0]

    scope = client.get(f"/api/resources/{first['id']}/hash-scope")
    response = client.post(
        f"/api/resources/{first['id']}/attach",
        json={
            "model": {"id": 71, "name": "Assigned model", "type": "LORA"},
            "version": {
                "id": 72,
                "name": "Assigned version",
                "hash_autov2": "B" * 10,
                "thumbnail_url": "https://example.invalid/thumbnail.jpg",
            },
        },
    )

    assert scope.json() == {"hash": file_hash, "images": 1}
    assert response.status_code == 200
    assert response.json()["hash"] == file_hash
    assert response.json()["reapplied"] == 1
    second = image_store.resources_for(second_image)[0]
    assert (
        second["model_id"],
        second["model_version_id"],
        second["model_name"],
        second["version_name"],
    ) == (71, 72, "Assigned model", "Assigned version")
    assignment = db.get_connection().execute(
        "SELECT resource_type, thumbnail_url FROM resource_map WHERE hash=?", (file_hash,)
    ).fetchone()
    assert dict(assignment) == {
        "resource_type": "lora",
        "thumbnail_url": "https://example.invalid/thumbnail.jpg",
    }


def test_the_count_shown_before_an_assignment_is_the_count_reported_after(client, scanned):
    """One picture with two rows for the same hash is one picture, twice over.

    The number is shown before the assignment and again after it. Counting
    images in one place and updated rows in the other made the second number
    contradict the first, for a reason nothing on screen could explain.
    """
    from backend.store import images as image_store

    file_hash = "E" * 10
    first_image, second_image = scanned
    image_store.replace_resources(
        first_image,
        [{"resource_type": "lora", "name_in_prompt": "First", "hash": file_hash}],
    )
    image_store.replace_resources(
        second_image,
        [
            {"resource_type": "lora", "name_in_prompt": "Twice a", "hash": file_hash},
            {"resource_type": "lora", "name_in_prompt": "Twice b", "hash": file_hash},
        ],
    )
    first = image_store.resources_for(first_image)[0]

    scope = client.get(f"/api/resources/{first['id']}/hash-scope").json()
    response = client.post(
        f"/api/resources/{first['id']}/attach",
        json={
            "model": {"id": 81, "name": "Assigned model", "type": "LORA"},
            "version": {"id": 82, "name": "Assigned version", "hash_autov2": "F" * 10},
        },
    ).json()

    assert scope["images"] == 1
    assert response["reapplied"] == scope["images"]
    assert [row["model_version_id"] for row in image_store.resources_for(second_image)] == [82, 82]


def test_attaching_a_resource_leaves_a_per_image_override_unchanged(client, scanned):
    from backend.store import images as image_store

    file_hash = "C" * 10
    first_image, second_image = scanned
    for image_id, name in ((first_image, "First"), (second_image, "Second")):
        image_store.replace_resources(
            image_id,
            [{"resource_type": "lora", "name_in_prompt": name, "hash": file_hash}],
        )
    first = image_store.resources_for(first_image)[0]
    second = image_store.resources_for(second_image)[0]
    image_store.update_resource(
        second["id"],
        {
            "model_id": 9,
            "model_version_id": 10,
            "model_name": "Protected model",
            "version_name": "Protected version",
            "locked_by_user": 1,
        },
    )

    response = client.post(
        f"/api/resources/{first['id']}/attach",
        json={
            "model": {"id": 71, "name": "Assigned model", "type": "LORA"},
            "version": {"id": 72, "name": "Assigned version", "hash_autov2": "D" * 10},
        },
    )

    assert response.json()["reapplied"] == 0
    protected = image_store.resource(second["id"])
    assert (protected["model_id"], protected["model_version_id"], protected["model_name"]) == (
        9,
        10,
        "Protected model",
    )


def test_attaching_a_resource_without_a_hash_does_not_reapply(client, scanned):
    from backend.store import images as image_store

    image_store.replace_resources(
        scanned[0], [{"resource_type": "lora", "name_in_prompt": "No hash"}]
    )
    resource = image_store.resources_for(scanned[0])[0]

    scope = client.get(f"/api/resources/{resource['id']}/hash-scope")
    response = client.post(
        f"/api/resources/{resource['id']}/attach",
        json={
            "model": {"id": 71, "name": "Assigned model", "type": "LORA"},
            "version": {"id": 72, "name": "Assigned version", "hash_autov2": "D" * 10},
        },
    )

    assert scope.json() == {"hash": None, "images": 0}
    assert response.json()["hash"] is None
    assert response.json()["reapplied"] == 0


def test_resource_hash_scope_reports_a_missing_resource(client):
    assert client.get("/api/resources/999/hash-scope").status_code == 404


def test_the_url_parser_keeps_its_refusals_in_order_for_its_other_caller():
    """`assign_model_file` maps these two exceptions to different error codes.

    The parser now reads the model id before checking for a version, because
    `ModelVersionRequired` carries it. If the id's *validity* check moved up with
    it, a URL like `/models/0` would answer `model_url_invalid` where it used to
    answer `model_version_required`, and the model-files endpoint would change
    its wording without anybody touching it.
    """
    from backend.resources import cache

    with pytest.raises(cache.ModelVersionRequired) as missing:
        cache.parse_model_url("https://civitai.com/models/0")
    assert missing.value.model_id == 0

    with pytest.raises(cache.InvalidModelUrl):
        cache.parse_model_url("https://civitai.com/models/0?modelVersionId=5")

    from backend.api.resources import model_search_lookup

    # An id of zero is not worth a request, so it falls through to the name search.
    assert model_search_lookup("https://civitai.com/models/0") == ("name", "https://civitai.com/models/0")
    assert model_search_lookup("https://civitai.com/models/7") == ("model", 7)


def test_preferred_creators_reorder_only_identical_hash_groups():
    from backend.civitai import rest

    items = [
        {"id": 1, "creator": "other", "versions": [{"hash_autov2": "same"}]},
        {"id": 2, "creator": "unrelated", "versions": [{"hash_autov2": "other"}]},
        {"id": 3, "creator": "CivitaiOfficial", "versions": [{"hash_autov2": "same"}]},
        {"id": 4, "creator": "mine", "versions": [{"hash_autov2": "same"}]},
    ]

    reordered = rest.prefer_creators(items, {"MINE", "civitaiofficial"})
    unique_hashes = rest.prefer_creators(items[:2], {"MINE", "civitaiofficial"})

    assert [item["id"] for item in reordered] == [4, 3, 1, 2]
    assert unique_hashes == items[:2]


def test_tags_are_normalised_and_deduplicated(client, scanned):
    post = client.post("/api/posts", json={"image_ids": scanned}).json()
    updated = client.put(
        f"/api/posts/{post['id']}/tags", json={"tags": ["Alpha", "alpha", " BETA ", ""]}
    ).json()
    assert updated["tags"] == ["alpha", "beta"]


def test_the_checklist_blocks_a_time_that_is_too_soon(client, scanned):
    from datetime import datetime, timedelta, timezone

    soon = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    post = client.post(
        "/api/posts",
        json={"image_ids": scanned, "schedule_mode": "absolute", "scheduled_at": soon},
    ).json()
    codes = {issue["code"] for issue in post["checklist"]["issues"]}
    assert "too_soon" in codes
    assert post["checklist"]["blocked"]


def test_publish_now_needs_an_explicit_acknowledgement(client, scanned):
    """It cannot be undone, so it is a warning that blocks until accepted."""
    post = client.post(
        "/api/posts", json={"image_ids": scanned, "publish_mode": "now"}
    ).json()
    codes = {issue["code"] for issue in post["checklist"]["issues"]}
    assert "publish_now" in codes
    assert post["checklist"]["can_push"] is False


@pytest.mark.parametrize(
    ("distance", "seed"),
    [(5, "3324970406"), (6, "458558052"), (4, "1779548050")],
)
def test_preflight_does_not_claim_reported_lookalikes_were_published(
    client, scanned, distance, seed
):
    from backend.store import images as image_store
    from backend.store import usage

    image = image_store.get(scanned[0])
    prompt = f"reported multi-image generation {seed}"
    with db.transaction() as conn:
        conn.execute(
            "UPDATE images SET parsed_json=? WHERE id=?",
            (json.dumps({"prompt": prompt, "fields": {"Seed": seed}}), image["id"]),
        )
    history_phash = f"{int(image['phash'], 16) ^ ((1 << distance) - 1):016x}"
    usage.record(
        sha256=f"history-{seed}",
        pixel_sha256=f"history-pixels-{seed}",
        phash=history_phash,
        post_id=None,
        remote_post_id=99,
        post_title="Earlier post",
        status=usage.PUBLISHED,
        seed=seed,
        prompt_hash=usage.prompt_fingerprint(prompt),
    )

    post = client.post(
        "/api/posts",
        json={
            "title": "Reported candidate",
            "image_ids": [image["id"]],
            "tags": ["test"],
            "schedule_offset_minutes": 120,
        },
    ).json()
    issues = post["checklist"]["issues"]

    assert "duplicate_exact" not in {issue["code"] for issue in issues}
    similar = next(issue for issue in issues if issue["code"] == "duplicate_similar")
    assert similar["level"] == "info"
    assert f"distance {distance}" in similar["message"]
    assert post["checklist"]["can_push"] is True


def test_unresolved_resources_are_named_without_blocking_the_push(client, scanned):
    from backend.store import images as image_store

    image_store.add_resource(
        scanned[0],
        {
            "resource_type": "lora",
            "name_in_prompt": "UnknownStyle",
            "hash": "ABCDEF1234",
        },
    )
    post = client.post(
        "/api/posts",
        json={
            "title": "T",
            "image_ids": [scanned[0]],
            "tags": ["x"],
            "schedule_offset_minutes": 120,
        },
    ).json()

    issue = next(
        item
        for item in post["checklist"]["issues"]
        if item["code"] == "unresolved_resources"
    )
    assert issue["level"] == "info"
    assert "UnknownStyle" in issue["message"]
    assert post["checklist"]["can_push"] is True


def test_the_push_preview_sends_nothing_and_lists_the_calls(client, scanned):
    post = client.post(
        "/api/posts",
        json={"image_ids": scanned, "tags": ["x"], "schedule_offset_minutes": 120},
    ).json()
    preview = client.post("/api/push/preview", json={"post_ids": [post["id"]]}).json()["items"][0]

    calls = [(call["transport"], call["call"]) for call in preview["calls"]]
    assert ("trpc", "post.create") in calls
    assert calls.count(("trpc", "post.addImage")) == len(scanned)
    assert calls.count(("mcp", "upload_image")) == len(scanned)
    assert calls.index(("trpc", "post.create")) < calls.index(("mcp", "upload_image"))
    assert calls.index(("trpc", "post.addImage")) < calls.index(("trpc", "post.update"))
    assert preview["resolved_publish_at"]


def test_spread_previews_before_it_applies(client, scanned):
    ids = [
        client.post("/api/posts", json={"image_ids": scanned}).json()["id"] for _ in range(3)
    ]
    plan = client.post(
        "/api/schedule/spread",
        json={"post_ids": ids, "start_offset_minutes": 90, "interval_minutes": 30},
    ).json()
    assert plan["applied"] is False
    assert [entry["offset_minutes"] for entry in plan["plan"]] == [90, 120, 150]

    # unchanged until applied
    assert client.get(f"/api/posts/{ids[0]}").json()["schedule_offset_minutes"] is None

    client.post(
        "/api/schedule/spread",
        json={
            "post_ids": ids,
            "start_offset_minutes": 90,
            "interval_minutes": 30,
            "apply": True,
        },
    )
    assert client.get(f"/api/posts/{ids[0]}").json()["schedule_offset_minutes"] == 90


def test_spread_refuses_the_whole_selection_when_one_post_is_pushing(client):
    from backend.posts import lifecycle
    from backend.store import posts as post_store

    pushing = post_store.create(title="Owned by a push")
    normal = post_store.create(title="Normal post")
    post_store.set_fields(pushing, state=lifecycle.PUSHING)

    response = client.post(
        "/api/schedule/spread",
        json={
            "post_ids": [normal, pushing],
            "start_offset_minutes": 90,
            "interval_minutes": 30,
            "apply": True,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "post_push_in_progress"
    assert post_store.get(normal)["schedule_offset_minutes"] is None
    assert post_store.get(pushing)["state"] == lifecycle.PUSHING


def test_spread_never_starts_below_the_minimum(client, scanned):
    post_id = client.post("/api/posts", json={"image_ids": scanned}).json()["id"]
    plan = client.post(
        "/api/schedule/spread",
        json={"post_ids": [post_id], "start_offset_minutes": 5, "interval_minutes": 30},
    ).json()
    assert plan["plan"][0]["offset_minutes"] >= 60


def test_the_calendar_reports_the_blocked_window(client):
    data = client.get("/api/schedule/calendar").json()
    assert data["min_lead_minutes"] == 60
    assert data["earliest"] > data["now"]


def test_offset_parsing_over_http(client):
    result = client.get("/api/schedule/parse-offset", params={"text": "3h 20min"}).json()
    assert result["minutes"] == 200
    assert result["label"] == "in 3 h 20 min"

    raised = client.get("/api/schedule/parse-offset", params={"text": "5"}).json()
    assert raised["raised"] is True, "a value below the floor is raised, not rejected"


def test_deleting_a_post_locally_keeps_the_remote_one(client, scanned):
    """Deleting a local plan must not remove the user's published work."""
    post = client.post("/api/posts", json={"image_ids": scanned}).json()
    from backend.store import posts as post_store

    post_store.set_fields(post["id"], remote_post_id=4242)

    response = client.delete(f"/api/posts/{post['id']}")
    assert response.status_code == 200
    assert client.get(f"/api/posts/{post['id']}").status_code == 404


def test_the_folder_browser_only_lists_directories(client, fixture_images):
    folder, _ = fixture_images
    listing = client.get("/api/browse-dirs", params={"path": str(folder.parent)}).json()
    names = {entry["name"] for entry in listing["entries"]}
    assert "images" in names
    assert not any(name.endswith(".png") for name in names)


def test_the_folder_browser_rejects_a_missing_path(client):
    assert client.get("/api/browse-dirs", params={"path": "/does/not/exist"}).status_code == 404


def test_jobs_can_be_polled_and_cancelled(client, fixture_images):
    folder, _ = fixture_images
    client.post("/api/sources", json={"path": str(folder)})
    job = client.post("/api/sources/scan").json()

    assert client.get(f"/api/jobs/{job['id']}").status_code == 200
    assert client.post(f"/api/jobs/{job['id']}/cancel").json()["ok"] is True
    assert client.get("/api/jobs/999999").status_code == 404


def test_adopt_batch_is_bounded_unique_and_reports_each_result(client, monkeypatch):
    from backend import jobs
    from backend.posts import sync

    assert (
        client.post("/api/sync/import/batch", json={"remote_post_ids": [501, 501]}).status_code
        == 422
    )
    assert (
        client.post(
            "/api/sync/import/batch", json={"remote_post_ids": list(range(1, 202))}
        ).status_code
        == 422
    )

    calls: list[int] = []

    def adopt(remote_post_id: int):
        calls.append(remote_post_id)
        return {"post_id": remote_post_id + 1000, "created": remote_post_id != 502}

    monkeypatch.setattr(sync, "import_remote", adopt)
    started = client.post(
        "/api/sync/import/batch", json={"remote_post_ids": [501, 502]}
    ).json()

    for _ in range(200):
        finished = jobs.get(started["id"])
        if finished is not None and finished.status not in ("starting", "running"):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("the adoption job never finished")

    body = client.get(f"/api/jobs/{started['id']}").json()
    assert calls == [501, 502]
    assert body["kind"] == "adopt"
    assert (body["processed"], body["succeeded"], body["skipped"], body["failed"]) == (
        2,
        1,
        1,
        0,
    )
    assert body["result"]["outcomes"] == [
        {
            "remote_post_id": 501,
            "post_id": 1501,
            "outcome": "created",
            "images": 0,
            "images_local": 0,
            "message": "",
        },
        {
            "remote_post_id": 502,
            "post_id": 1502,
            "outcome": "skipped",
            "images": 0,
            "images_local": 0,
            "message": "",
        },
    ]


# --- Domain ------------------------------------------------------------------


def test_the_default_domain_is_red_not_com(client):
    """civitai.com filters to SFW in some regions - a listing from there can omit
    images that exist."""
    from backend import config

    assert config.site_base() == "https://civitai.red"
    assert client.get("/api/settings").json()["site_base"] == "https://civitai.red"


def test_the_domain_drives_queries_not_only_links(client):
    from backend import config

    client.put("/api/settings", json={"site_base": "https://civitai.com"})
    assert config.rest_base() == "https://civitai.com/api/v1"
    assert config.trpc_base() == "https://civitai.com/api/trpc"


def test_the_upload_host_stays_fixed(client):
    """mcp.civitai.red does not exist - the host does not resolve."""
    from backend import config

    client.put("/api/settings", json={"site_base": "https://civitai.red"})
    assert config.MCP_URL == "https://mcp.civitai.com/mcp"


def test_an_address_without_scheme_is_refused(client):
    assert client.put("/api/settings", json={"site_base": "civitai.red"}).status_code == 400


def test_an_empty_value_falls_back_to_the_default(client):
    from backend import config

    client.put("/api/settings", json={"site_base": "https://civitai.com"})
    client.put("/api/settings", json={"site_base": ""})
    assert config.site_base() == config.DEFAULT_SITE_BASE


def test_a_trailing_slash_does_not_produce_a_double_slash(client):
    from backend import config

    client.put("/api/settings", json={"site_base": "https://civitai.com/"})
    assert config.rest_base() == "https://civitai.com/api/v1"


def test_changing_the_domain_drops_the_cached_account(client):
    """The answer could differ when it comes from the other domain."""
    from backend import db

    db.set_json_setting("account_json", {"username": "alt"})
    client.put("/api/settings", json={"site_base": "https://civitai.com"})
    assert db.get_json_setting("account_json") is None


def test_an_image_url_is_built_from_the_upload_key(client):
    """post.getEdit returns only the key - there is no other way to display an
    image without a local file."""
    from backend import config

    url = config.image_url("fdf76653-8382-49fc-95a0-b4ab3bc4972d", width=450)
    assert url.startswith("https://image.civitai.com/")
    assert url.endswith("/fdf76653-8382-49fc-95a0-b4ab3bc4972d/width=450")
    assert config.image_url("abc", original=True).endswith("/abc/original=true")


def test_the_folder_browser_lists_a_typed_path(client, fixture_images):
    """While typing, the folder above the partial name is listed and filtered by
    its prefix - that happens in the browser; all that matters here is that the
    listing accepts every intermediate directory."""
    folder, _ = fixture_images
    listing = client.get("/api/browse-dirs", params={"path": str(folder.parent)}).json()

    assert listing["path"] == str(folder.parent)
    assert any(entry["name"] == "images" for entry in listing["entries"])
    assert listing["parent"], "going up works too"


def test_a_typed_path_with_a_trailing_slash_still_works(client, fixture_images):
    folder, _ = fixture_images
    assert client.get("/api/browse-dirs", params={"path": f"{folder}/"}).status_code == 200


def test_a_typo_gives_a_clear_status_not_a_crash(client):
    """The UI shows this as a line under the field, not as a crash."""
    response = client.get("/api/browse-dirs", params={"path": "/mnt/y/Genereate"})
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "folder_not_found"


def test_an_unknown_api_path_is_a_404_not_the_app_shell(client):
    """A tab left open across an update calls endpoints that no longer exist.
    Answering those with 200 and a page of HTML turns it into a JSON parse error
    somewhere far from the cause."""
    response = client.get("/api/settings/metadata-db")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"]["code"] == "unknown_endpoint"


def test_an_unknown_page_still_gets_the_app_shell(client):
    """Anything that is not /api is a client-side route."""
    response = client.get("/some/deep/link")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_a_negative_limit_is_refused_rather_than_meaning_unlimited(client, scanned):
    """SQLite reads LIMIT -1 as no limit, so min(limit, 500) let one query pull
    the whole library and run the duplicate check over all of it."""
    assert client.get("/api/images", params={"limit": -1}).status_code == 422
    assert client.get("/api/images", params={"limit": 99999}).status_code == 422
    assert client.get("/api/images", params={"offset": -5}).status_code == 422

    ok = client.get("/api/images", params={"limit": 5})
    assert ok.status_code == 200
    assert ok.json()["limit"] == 5, "and the answer reports what was used"


def test_an_image_in_a_draft_is_planned_and_leaves_the_history_alone(client, scanned):
    """Planning is intent, not history.

    `image_usage` is the published record and `IMG-13` makes it append-only, so
    the flag is derived from `post_images` and the post's state instead. The
    proof that it really is derived: deleting the draft clears it, which a
    usage row would never do.
    """
    from backend import db

    def usage_rows() -> int:
        return db.get_connection().execute("SELECT COUNT(*) FROM image_usage").fetchone()[0]

    image_id = scanned[0]
    before = usage_rows()

    post = client.post("/api/posts", json={"image_ids": [image_id]}).json()
    listed = client.get("/api/images", params={"usage_state": "all"}).json()["items"]
    planned = next(item for item in listed if item["id"] == image_id)
    assert planned["planned"] is True
    assert planned["used"] is False, "planned is not the same fact as posted"

    ids = {
        item["id"]
        for item in client.get("/api/images", params={"usage_state": "planned"}).json()["items"]
    }
    assert image_id in ids
    ids = {
        item["id"]
        for item in client.get("/api/images", params={"usage_state": "unused"}).json()["items"]
    }
    assert image_id not in ids, "a planned image is not available"

    assert client.delete(f"/api/posts/{post['id']}").status_code in (200, 204)
    listed = client.get("/api/images", params={"usage_state": "all"}).json()["items"]
    assert next(item for item in listed if item["id"] == image_id)["planned"] is False
    assert usage_rows() == before, "IMG-13: the history was touched"


def test_an_image_in_a_published_post_is_not_planned(client, scanned):
    from backend.posts import lifecycle
    from backend.store import posts as post_store

    image_id = scanned[0]
    post = client.post("/api/posts", json={"image_ids": [image_id]}).json()
    post_store.set_fields(post["id"], state=lifecycle.PUBLISHED)

    listed = client.get("/api/images", params={"usage_state": "all"}).json()["items"]
    row = next(item for item in listed if item["id"] == image_id)
    assert row["planned"] is False, "a published post is not a plan"


def test_archiving_a_selection_reports_the_post_it_may_not_move(client, scanned):
    """A selection must not archive some and drop the rest without a word.

    The refused case is a **scheduled** post, not a draft: `TRANSITIONS` has no
    edge from `scheduled` to `archived`, while `draft` and `ready` do have one
    by design. A post on its way to CivitAI cannot be swept off the board.
    """
    from backend.posts import lifecycle
    from backend.store import posts as post_store

    published = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()
    post_store.set_fields(published["id"], state=lifecycle.PUBLISHED)
    scheduled = client.post("/api/posts", json={"image_ids": [scanned[1]]}).json()
    post_store.set_fields(scheduled["id"], state=lifecycle.SCHEDULED)

    response = client.post(
        "/api/posts/archive-selected",
        json={"post_ids": [published["id"], scheduled["id"]]},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["archived"] == 1
    assert body["post_ids"] == [published["id"]]
    assert [item["post_id"] for item in body["refused"]] == [scheduled["id"]]
    assert body["refused"][0]["code"] in lifecycle.TRANSITION_ERROR_CODES

    assert post_store.get(published["id"])["state"] == lifecycle.ARCHIVED
    assert post_store.get(scheduled["id"])["state"] == lifecycle.SCHEDULED, "untouched"


def test_archiving_a_selection_moves_no_files(client, scanned, tmp_path):
    """State only. `ARC-04`: files move on an explicit archive run, never here."""
    from pathlib import Path

    from backend.posts import lifecycle
    from backend.store import images as image_store
    from backend.store import posts as post_store

    post = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()
    post_store.set_fields(post["id"], state=lifecycle.PUBLISHED)
    original = Path(image_store.get(scanned[0])["absolute_path"])
    assert original.exists()

    client.post("/api/posts/archive-selected", json={"post_ids": [post["id"]]})

    assert original.exists(), "the file stayed where it was"
    assert post_store.get(post["id"])["state"] == lifecycle.ARCHIVED


def test_a_published_post_civitai_lost_can_still_be_archived(client, scanned):
    """The dead end the conditional edge exists to prevent.

    A post that went out and then vanished from CivitAI becomes
    `remote_missing`. Without a way into the archive it would sit under "needs
    attention" for ever, and the historical record the archive is for would be
    the one thing that cannot be kept.
    """
    from backend.posts import lifecycle
    from backend.store import posts as post_store

    post = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()
    post_store.set_fields(
        post["id"],
        state=lifecycle.REMOTE_MISSING,
        remote_published_at="2026-08-20T10:00:00Z",
    )

    body = client.post(
        "/api/posts/archive-selected", json={"post_ids": [post["id"]]}
    ).json()

    assert body["archived"] == 1
    assert body["refused"] == []
    assert post_store.get(post["id"])["state"] == lifecycle.ARCHIVED


def test_a_pushing_post_keeps_its_files_and_its_credits(client, scanned):
    """The two doors the task's brief did not name, closed against the same rule.

    Both were found by sweeping the API surface for what writes to a post rather
    than for which file a route lives in. Permanent deletion is the one action in
    this application that cannot be walked back, and a resource row is what the
    uploaded CivitAI block credits - so changing one mid-push changes what the
    run is sending.
    """
    from backend.posts import lifecycle
    from backend.store import images as image_store
    from backend.store import posts as post_store

    image_id = scanned[0]
    post_id = post_store.create(title="Owned by a push")
    post_store.set_images(post_id, [image_id])
    post_store.set_fields(post_id, state=lifecycle.PUSHING)

    resource_id = image_store.add_resource(
        image_id, {"resource_type": "lora", "name_in_prompt": "Held"}
    )["id"]

    refusals = [
        client.post("/api/images/delete", json={"image_ids": [image_id]}),
        client.post("/api/images/delete-plan", json={"image_ids": [image_id]}),
        client.post(
            f"/api/images/{image_id}/resources",
            json={"resource_type": "lora", "name": "Added mid-push"},
        ),
        client.patch(f"/api/resources/{resource_id}", json={"name": "Renamed mid-push"}),
        client.post(f"/api/resources/{resource_id}/unlock"),
        client.delete(f"/api/resources/{resource_id}"),
        client.post(f"/api/resources/{resource_id}/restore"),
    ]
    for response in refusals:
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "post_push_in_progress"

    assert image_store.resource(resource_id)["name_in_prompt"] == "Held"
    assert Path(image_store.get(image_id)["absolute_path"]).exists()

    # And the same calls work again once the run has let go, which is what makes
    # this a wait rather than a wall.
    post_store.set_fields(post_id, state=lifecycle.PUBLISHED)
    renamed = client.patch(f"/api/resources/{resource_id}", json={"name": "Renamed"})
    assert renamed.status_code == 200
