from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from backend import jobs
from backend.llm import service as llm_service
from backend.metadata import (
    compose,
    editing,
    infotext,
    linter,
    prompt_tools,
    resource_edits,
    upload_document,
)
from backend.store import edits

IMAGE_DRAWER_RESOURCE_ACTION_CHECK = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "assert_image_drawer_resource_action.mjs"
)
IMAGE_DRAWER_RESOURCE_ACTION_SKIP_REASON = (
    "drawer Svelte assertion requires node and its helper, "
    "which the release export smoke test deliberately does without"
)


def _sha(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _scanned_image_id(client, filename: str) -> int:
    images = client.get("/api/images").json()["items"]
    return next(image["id"] for image in images if image["relative_path"] == filename)


def test_edit_api_returns_diff_and_never_changes_source(client, scanned):
    image_id = scanned[0]
    detail = client.get(f"/api/images/{image_id}").json()
    before_sha = _sha(detail["absolute_path"])
    state = client.get(f"/api/images/{image_id}/edit").json()
    original = state["original"]

    response = client.put(
        f"/api/images/{image_id}/edit",
        json={
            "draft": {
                "prompt": "task fourteen prompt",
                "fields": {"Steps": 17, "New field": "kept"},
            },
            "touched": ["prompt", "Steps", "New field"],
            "deleted": ["Sampler"],
        },
    )
    assert response.status_code == 200
    edited = response.json()
    assert edited["effective"]["prompt"] == "task fourteen prompt"
    assert edited["effective"]["fields"]["Steps"] == 17
    assert edited["effective"]["fields"]["New field"] == "kept"
    assert "Sampler" not in edited["effective"]["fields"]
    assert edited["diff"]["changed"] is True
    assert edited["diff"]["prompts"]["prompt"] == {
        "original": original["prompt"],
        "value": "task fourteen prompt",
        "changed": True,
        "deleted": False,
    }
    assert _sha(detail["absolute_path"]) == before_sha
    assert client.get("/api/images").json()["items"][0]["edited"] is True


def test_equal_values_are_not_stored_as_edits(client, scanned):
    image_id = scanned[0]
    state = client.get(f"/api/images/{image_id}/edit").json()
    original = state["original"]
    response = client.put(
        f"/api/images/{image_id}/edit",
        json={
            "draft": {
                "prompt": original["prompt"],
                "fields": {"Steps": original["fields"]["Steps"]},
            },
            "touched": ["prompt", "Steps"],
            "deleted": [],
        },
    )
    assert response.status_code == 200
    assert edits.get(image_id) is None


@pytest.mark.parametrize(
    "override",
    ("added_by_user", "deleted_by_user", "locked_by_user", None),
    ids=("added", "deleted", "locked", "unchanged"),
)
def test_resource_overrides_mark_both_image_apis_as_edited(client, scanned, override):
    from backend.store import images as image_store

    image_id = scanned[0]
    if override == "added_by_user":
        image_store.add_resource(
            image_id,
            {"resource_type": "lora", "name_in_prompt": "Manual LoRA"},
        )
    elif override is not None:
        resource = image_store.resources_for(image_id)[0]
        image_store.update_resource(resource["id"], {override: 1})

    assert edits.get(image_id) is None
    library_image = next(
        item for item in client.get("/api/images").json()["items"] if item["id"] == image_id
    )
    expected = override is not None
    assert library_image["edited"] is expected
    assert client.get(f"/api/images/{image_id}").json()["edited"] is expected


def test_bulk_preview_then_apply_reaches_every_selected_image(client, scanned):
    payload = {
        "image_ids": scanned,
        "operations": {
            "prompt_prepend": "shared opening",
            "fields": {"CFG scale": 6.5},
        },
        "apply": False,
    }
    preview = client.post("/api/images/edit/bulk", json=payload)
    assert preview.status_code == 200
    assert len(preview.json()["plan"]) == len(scanned)
    assert all(item["changes"] for item in preview.json()["plan"])
    assert edits.count() == 0

    payload["apply"] = True
    applied = client.post("/api/images/edit/bulk", json=payload)
    assert applied.status_code == 200
    assert applied.json()["changed"] == len(scanned)
    assert edits.edited_ids(scanned) == set(scanned)
    for image_id in scanned:
        effective = client.get(f"/api/images/{image_id}/edit").json()["effective"]
        assert effective["prompt"].startswith("shared opening,")
        assert effective["fields"]["CFG scale"] == 6.5


def test_bulk_edit_can_take_a_whole_post_and_marks_post_cards(client, scanned):
    post = client.post("/api/posts", json={"image_ids": scanned}).json()
    preview = client.post(
        "/api/images/edit/bulk",
        json={
            "post_id": post["id"],
            "operations": {"prompt_append": "shared ending"},
            "apply": True,
        },
    )
    assert preview.status_code == 200
    assert preview.json()["changed"] == len(scanned)
    cards = client.get(f"/api/posts/{post['id']}").json()["images"]
    assert all(card["edited"] for card in cards)


def test_bulk_edit_refuses_all_images_when_one_belongs_to_a_pushing_post(
    client, scanned
):
    from backend.posts import lifecycle
    from backend.store import posts as post_store

    post_id = post_store.create(title="Owned metadata")
    post_store.set_images(post_id, [scanned[0]])
    post_store.set_fields(post_id, state=lifecycle.PUSHING)

    response = client.post(
        "/api/images/edit/bulk",
        json={
            "image_ids": scanned,
            "operations": {"prompt_append": "must not be applied"},
            "apply": True,
        },
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "post_push_in_progress"
    assert "Owned metadata" in detail["message"]
    assert edits.edited_ids(scanned) == set()


def test_single_image_edit_refuses_an_image_in_a_pushing_post(client, scanned):
    from backend.posts import lifecycle
    from backend.store import posts as post_store

    post_id = post_store.create(title="Owned metadata")
    post_store.set_images(post_id, [scanned[0]])
    post_store.set_fields(post_id, state=lifecycle.PUSHING)

    response = client.put(
        f"/api/images/{scanned[0]}/edit",
        json={
            "draft": {"prompt": "must not be applied"},
            "touched": ["prompt"],
            "deleted": [],
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "post_push_in_progress"
    assert edits.edited_ids(scanned) == set()


def test_resource_overrides_are_projected_without_storing_an_edit(client, scanned):
    image_id = scanned[0]
    added = client.post(
        f"/api/images/{image_id}/resources",
        json={"resource_type": "lora", "name": "Manual LoRA"},
    )
    assert added.status_code == 200
    resource = added.json()
    assert resource["added_by_user"] == 1

    attached = client.post(
        f"/api/resources/{resource['id']}/attach",
        json={
            "model": {"id": 12, "name": "Manual model"},
            "version": {"id": 34, "name": "v1", "hash_autov2": "ABCDEF1234"},
        },
    )
    assert attached.status_code == 200
    assert attached.json()["resource"]["locked_by_user"] == 1
    assert edits.get(image_id) is None
    from backend.scanner import service as scanner

    scanner.scan_roots(jobs.Job(id=0, kind="scan"))
    surviving = next(
        row
        for row in client.get(f"/api/images/{image_id}/edit").json()["resources"]
        if row["name_in_prompt"] == "Manual LoRA"
    )
    assert surviving["model_version_id"] == 34
    assert surviving["hash"] == "ABCDEF1234"
    effective = client.get(f"/api/images/{image_id}/edit").json()["effective"]["fields"]
    assert "Hashes" not in effective, "the generator did not write that representation"
    assert any(
        item.get("modelVersionId") == 34 for item in effective["Civitai resources"]
    )

    deleted = client.delete(f"/api/resources/{resource['id']}")
    assert deleted.status_code == 200
    assert all(
        row["id"] != resource["id"]
        for row in client.get(f"/api/images/{image_id}/edit").json()["resources"]
    )
    assert client.post(f"/api/resources/{resource['id']}/restore").status_code == 404


def _wait_for_api_job(client, started: dict, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    current = started
    while current["status"] in ("starting", "running") and time.monotonic() < deadline:
        time.sleep(0.01)
        current = client.get(f"/api/jobs/{started['id']}").json()
    assert current["status"] == "done", current
    return current


def _resource_key(row: dict) -> tuple[str, str]:
    return row["resource_type"], row["name_in_prompt"]


def _resource_sequence_image(client, tmp_path: Path) -> dict:
    folder = tmp_path / "resource-sequence"
    folder.mkdir()
    path = folder / "resource.png"
    raw = (
        "a test image, <lora:FileLora:0.7>\n"
        "Steps: 8, Sampler: Euler, CFG scale: 1, Model: ExternalModel, "
        'Model hash: CCCCCCCCCC, Lora hashes: "FileLora: AAAAAAAAAA"'
    )
    info = PngImagePlugin.PngInfo()
    info.add_text("parameters", raw)
    Image.new("RGB", (16, 16), color=(40, 80, 120)).save(path, pnginfo=info)
    source = client.post("/api/sources", json={"path": str(folder), "label": "Resources"})
    assert source.status_code == 200
    started = client.post("/api/sources/scan", params={"source_id": source.json()["id"]})
    assert started.status_code == 200
    _wait_for_api_job(client, started.json())
    image = next(
        row
        for row in client.get("/api/images").json()["items"]
        if row["absolute_path"] == str(path)
    )
    drawer = client.get(f"/api/images/{image['id']}/edit").json()["resources"]
    file_row = next(row for row in drawer if row["name_in_prompt"] == "FileLora")
    attached = client.post(
        f"/api/resources/{file_row['id']}/attach",
        json={
            "model": {"id": 10, "name": "File model"},
            "version": {"id": 101, "name": "file", "hash_autov2": "AAAAAAAAAA"},
        },
    )
    assert attached.status_code == 200
    _add_sequence_resource(client, image["id"])
    return {
        "id": image["id"],
        "path": path,
        "source_id": source.json()["id"],
        "original": {_resource_key(row) for row in drawer},
        "added": {("lora", "AddedLora")},
        "deleted": set(),
        "versions": {("lora", "FileLora"): 101, ("lora", "AddedLora"): 202},
    }


def _add_sequence_resource(client, image_id: int) -> None:
    added = client.post(
        f"/api/images/{image_id}/resources",
        json={"resource_type": "lora", "name": "AddedLora", "weight": 0.25},
    )
    assert added.status_code == 200
    attached = client.post(
        f"/api/resources/{added.json()['id']}/attach",
        json={
            "model": {"id": 20, "name": "Added model"},
            "version": {"id": 202, "name": "added", "hash_autov2": "BBBBBBBBBB"},
        },
    )
    assert attached.status_code == 200


def _assert_resource_oracle(client, state: dict) -> None:
    from backend.posts import materialise
    from backend.store import images as image_store

    expected_active = state["original"] - state["deleted"] | state["added"]
    image = image_store.get(state["id"])
    assert image is not None
    projected = infotext.parse(
        resource_edits.project(
            image["raw_infotext"], image["raw_infotext"], state["id"]
        )
    )
    fields = projected["fields"]
    projected_resources = {
        ("lora", name) for name in infotext.parse_hash_map(fields.get("Lora hashes"))
    }
    if fields.get("Model"):
        projected_resources.add(("checkpoint", fields["Model"]))
    assert projected_resources == expected_active

    upload = materialise.upload_document(image)
    assert upload is not None
    actual_versions = {
        (entry["type"], entry["modelVersionId"])
        for entry in upload.get("civitaiResources", [])
    }
    expected_versions = {
        (key[0], version)
        for key, version in state["versions"].items()
        if key in expected_active
    }
    assert actual_versions == expected_versions

    drawer = client.get(f"/api/images/{state['id']}/edit").json()["resources"]
    expected_drawer = state["original"] | state["added"]
    assert {_resource_key(row) for row in drawer} == expected_drawer
    assert {
        _resource_key(row) for row in drawer if row["deleted_by_user"]
    } == state["deleted"]
    assert not any(row["added_by_user"] and row["deleted_by_user"] for row in drawer)


@pytest.mark.parametrize(
    "sequence",
    (
        (
            ("delete", ("lora", "FileLora")),
            ("scan", None),
            ("restore", ("lora", "FileLora")),
            ("delete", ("lora", "FileLora")),
            ("restore", ("lora", "FileLora")),
        ),
        (
            ("delete", ("lora", "AddedLora")),
            ("add", ("lora", "AddedLora")),
            ("scan", None),
            ("restore", ("lora", "AddedLora")),
            ("delete", ("lora", "AddedLora")),
            ("add", ("lora", "AddedLora")),
        ),
    ),
    ids=("file-row-transitions", "added-row-transitions"),
)
def test_resource_action_sequences_match_one_oracle_after_every_http_step(
    client, tmp_path, sequence
):
    """Original - deleted + added governs projection, upload and drawer."""
    state = _resource_sequence_image(client, tmp_path)
    _assert_resource_oracle(client, state)

    for action, key in sequence:
        if action == "scan":
            stat = state["path"].stat()
            os.utime(
                state["path"],
                ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000),
            )
            response = client.post(
                "/api/sources/scan", params={"source_id": state["source_id"]}
            )
            assert response.status_code == 200
            _wait_for_api_job(client, response.json())
        elif action == "add":
            _add_sequence_resource(client, state["id"])
            state["added"].add(key)
        else:
            rows = client.get(f"/api/images/{state['id']}/edit").json()["resources"]
            target = next(row for row in rows if _resource_key(row) == key)
            if action == "delete":
                response = client.delete(f"/api/resources/{target['id']}")
                if key in state["original"]:
                    state["deleted"].add(key)
                else:
                    state["added"].discard(key)
            else:
                response = client.post(f"/api/resources/{target['id']}/restore")
                state["deleted"].discard(key)
            assert response.status_code == 200
        _assert_resource_oracle(client, state)


def test_delete_holds_the_origin_read_and_mutation_against_a_resource_rebuild(
    client, scanned, monkeypatch
):
    """A rebuild begins at the old read/write boundary and cannot interleave."""
    from backend import db
    from backend.metadata import resources
    from backend.store import images as image_store

    image_id = scanned[0]
    added = client.post(
        f"/api/images/{image_id}/resources",
        json={"resource_type": "lora", "name": "ConcurrentAdded"},
    ).json()
    image = image_store.get(image_id)
    assert image is not None
    rebuilt = resources.extract(infotext.parse(image["raw_infotext"]))
    original_get_connection = db.get_connection
    read_seen = threading.Event()
    rebuild_started = threading.Event()
    rebuild_done = threading.Event()
    read_was_in_transaction: list[bool] = []
    traced_connections: set[int] = set()
    trace_lock = threading.Lock()
    triggered = False

    def traced_get_connection():
        nonlocal triggered
        conn = original_get_connection()
        with trace_lock:
            if id(conn) in traced_connections:
                return conn
            traced_connections.add(id(conn))

        def trace(statement: str) -> None:
            nonlocal triggered
            if "SELECT * FROM image_resources WHERE id=" not in statement:
                return
            with trace_lock:
                if triggered:
                    return
                triggered = True
            inside = conn.in_transaction
            read_was_in_transaction.append(inside)
            read_seen.set()
            assert rebuild_started.wait(5)
            if not inside:
                assert rebuild_done.wait(5)

        conn.set_trace_callback(trace)
        return conn

    monkeypatch.setattr(db, "get_connection", traced_get_connection)

    def rebuild() -> None:
        assert read_seen.wait(5)
        rebuild_started.set()
        try:
            image_store.replace_resources(image_id, rebuilt)
        finally:
            rebuild_done.set()
            db.close_connection()

    thread = threading.Thread(target=rebuild, daemon=True)
    thread.start()
    response = client.delete(f"/api/resources/{added['id']}")
    thread.join(timeout=5)

    assert response.status_code == 200
    assert read_was_in_transaction == [True]
    assert rebuild_done.is_set()
    assert not any(
        row["name_in_prompt"] == "ConcurrentAdded"
        for row in image_store.resources_for(image_id)
    )


def test_legacy_added_tombstone_is_not_a_restore_action_in_the_drawer(request):
    from backend.store import images as image_store

    if (
        shutil.which("node") is None
        or not IMAGE_DRAWER_RESOURCE_ACTION_CHECK.is_file()
    ):
        pytest.skip(IMAGE_DRAWER_RESOURCE_ACTION_SKIP_REASON)

    client = request.getfixturevalue("client")
    scanned = request.getfixturevalue("scanned")
    image_id = scanned[0]
    added = client.post(
        f"/api/images/{image_id}/resources",
        json={"resource_type": "lora", "name": "LegacyAdded"},
    ).json()
    image_store.update_resource(added["id"], {"deleted_by_user": 1})
    drawer = client.get(f"/api/images/{image_id}/edit").json()["resources"]
    legacy = next(row for row in drawer if row["id"] == added["id"])
    subprocess.run(
        [
            "node",
            str(IMAGE_DRAWER_RESOURCE_ACTION_CHECK),
            json.dumps(legacy),
            "metadata.removeAdded",
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=True,
        capture_output=True,
        text=True,
    )
    restored = client.post(f"/api/resources/{legacy['id']}/restore")
    assert restored.status_code == 200
    assert restored.json() == {}
    assert all(
        row["id"] != legacy["id"]
        for row in client.get(f"/api/images/{image_id}/edit").json()["resources"]
    )


def test_prompt_tools_and_linter_cover_migrated_behaviour():
    assert prompt_tools.find_replace("Blue CAT", "cat", "fox") == "Blue fox"
    assert prompt_tools.join_prompt("middle", "first,", ", last") == "first, middle, last"
    cleaned = prompt_tools.clean_for_shortening(
        "masterpiece, <lora:foo:1>, (red cat:1.2), CAPTION_HERE",
        ["masterpiece"],
    )
    assert cleaned == "red cat"
    assert prompt_tools.clean_shortening_result('Here is the sentence: "A red cat sleeps."') == (
        "A red cat sleeps."
    )

    warnings = linter.lint(
        {
            "prompt": "CAPTION_HERE, <lora:unknown:1>",
            "negative_prompt": None,
            "fields": {"Model": "Base", "Size": "512x512", "Prompt Enhancer": "placeholder"},
        }
    )
    codes = {warning["code"] for warning in warnings}
    assert codes == {
        "UNSUBSTITUTED_TEMPLATE_TOKEN",
        "MISSING_MODEL_HASH",
    }


def test_prompt_exclusions_cover_generated_upload_prompts_and_leave_the_original(
    client, scanned, tmp_path
):
    from backend.metadata import png_io, resources
    from backend.posts import materialise
    from backend.store import images as image_store

    image = next(
        image_store.get(image_id)
        for image_id in scanned
        if "mobe-dedafa" in (image_store.get(image_id) or {}).get("raw_infotext", "")
    )
    original_text = image["raw_infotext"]
    original_sha = _sha(image["absolute_path"])
    for version_id, row in enumerate(image_store.resources_for(image["id"]), start=9000):
        if row["resource_type"] in (resources.LORA, resources.EMBEDDING):
            image_store.update_resource(row["id"], {"model_version_id": version_id})
    baseline = infotext.parse(materialise.upload_infotext(image))
    baseline_meta = materialise.upload_document(image)

    invalid = client.put("/api/settings", json={"prompt_exclusions": ["["]})
    assert invalid.status_code == 400
    assert invalid.json()["detail"]["code"] == "invalid_prompt_exclusion"
    assert "unterminated character set" in invalid.json()["detail"]["params"]["reason"]
    assert client.get("/api/settings").json()["prompt_exclusions"] == []

    exclusions = [r"mobe-dedafa", r"<lora:[^>]+>"]
    response = client.put("/api/settings", json={"prompt_exclusions": exclusions})
    upload_text = materialise.upload_infotext(image)
    upload_meta = materialise.upload_document(image)
    image["source_path"] = image["absolute_path"]
    target = materialise.path_for(image, tmp_path / "upload", upload_text)
    projected = infotext.parse(png_io.read(target).infotext)

    assert response.status_code == 200
    assert response.json()["prompt_exclusions"] == exclusions
    assert "mobe-dedafa" not in projected["prompt"]
    assert projected["negative_prompt"] == (
        "bad quality, worst quality, low quality, lowres, jpeg artifacts, bad anatomy, "
        "bad hands, signature, watermark, artist,"
    )
    assert "<lora:" not in projected["prompt"]
    assert {
        key: value
        for key, value in projected["fields"].items()
        if not key.casefold().endswith("prompt")
    } == {
        key: value
        for key, value in baseline["fields"].items()
        if not key.casefold().endswith("prompt")
    }
    assert upload_meta is not None and baseline_meta is not None
    assert upload_meta.get("civitaiResources") == baseline_meta.get("civitaiResources")
    assert image_store.get(image["id"])["raw_infotext"] == original_text
    assert _sha(image["absolute_path"]) == original_sha


def test_shorten_creates_suggestions_but_does_not_write(monkeypatch, scanned):
    class Worker:
        def __init__(self):
            self.info = {"device": "test"}

        def generate(self, _request):
            return "A compact scene."

    monkeypatch.setattr(llm_service, "start_worker", lambda: Worker())
    monkeypatch.setattr(llm_service, "stop_worker", lambda: None)
    job = jobs.Job(id=0, kind="llm")
    result = llm_service.shorten_prompts(job, scanned)
    assert len(result["items"]) == len(scanned)
    assert all(item["suggestion"] == "A compact scene." for item in result["items"])
    assert edits.count() == 0


def test_an_unedited_image_reports_an_edit_of_the_same_shape(client, scanned):
    """The drawer reads `edit.draft.prompt`. Handing it a bare draft instead of an
    edit made every first change to an untouched image throw."""
    unedited = client.get(f"/api/images/{scanned[0]}/edit").json()["edit"]

    assert {"draft", "touched", "deleted"} <= set(unedited)
    assert unedited["touched"] == [] and unedited["deleted"] == []
    assert unedited["draft"]["prompt"] is None

    client.put(
        f"/api/images/{scanned[0]}/edit",
        json={"draft": {"prompt": "changed"}, "touched": ["prompt"], "deleted": []},
    )
    edited = client.get(f"/api/images/{scanned[0]}/edit").json()["edit"]
    assert {"draft", "touched", "deleted"} <= set(edited), "and the shape does not change"


def test_resetting_an_edit_keeps_the_resource_projection_honest(client, scanned):
    """Resetting ordinary edits does not affect the independent resource projection."""
    image_id = scanned[0]
    resources = client.get(f"/api/images/{image_id}/edit").json()["resources"]
    if not resources:
        pytest.skip("the fixture image has no resources to override")

    client.delete(f"/api/resources/{resources[0]['id']}")
    after_delete = client.get(f"/api/images/{image_id}/edit").json()

    client.put(
        f"/api/images/{image_id}/edit",
        json={"draft": {}, "touched": [], "deleted": []},
    )
    after_reset = client.get(f"/api/images/{image_id}/edit").json()

    still_deleted = [r for r in after_reset["resources"] if r["deleted_by_user"]]
    assert still_deleted, "the flag survives a reset"
    assert after_reset["effective"]["fields"].get("Civitai resources") == after_delete[
        "effective"
    ]["fields"].get("Civitai resources"), "and so does what would be uploaded"


def test_a_hash_row_keeps_the_identity_reported_by_its_lookup(monkeypatch):
    from backend.metadata import resources
    from backend.resources import cache

    cached_identity = {
        "model_id": 10,
        "model_version_id": 101,
        "model_name": "Cached Alpha",
        "version_name": "Cached v1",
        "thumbnail_url": None,
    }

    def lookup(digest):
        if resources.hash_prefix(digest) == "AAAAAAAAAA":
            return cached_identity
        return None

    monkeypatch.setattr(cache, "get", lookup)
    row_fields = {
        "known_hash": {"Lora hashes": {"Alpha": "AAAAAAAAAA"}},
        "unknown_hash": {"Lora hashes": {"Alpha": "CCCCCCCCCC"}},
        "no_hash": {},
    }
    credits = {
        "carries_hash": {
            "type": "lora",
            "hash": "BBBBBBBBBB",
            "weight": 0.2,
            "modelId": 20,
            "modelVersionId": 202,
            "modelName": "Alpha",
            "modelVersionName": "Claimed v2",
        },
        "matching_version": {
            "type": "lora",
            "weight": 0.3,
            "modelId": 10,
            "modelVersionId": 101,
            "modelName": "Cached Alpha",
            "modelVersionName": "Cached v1",
        },
        "name_only": {"type": "lora", "weight": 0.4, "modelName": "ALPHA"},
        "matches_nothing": {
            "type": "lora",
            "weight": 0.5,
            "modelVersionId": 404,
            "modelName": "Gamma",
        },
    }
    identity_fields = ("model_id", "model_version_id", "model_name", "version_name")

    for row_kind, fields in row_fields.items():
        baseline = resources.extract({"prompt": "", "fields": fields})
        cache.enrich(baseline)
        for credit_kind, credit in credits.items():
            parsed = {
                "prompt": "",
                "fields": {**fields, "Civitai resources": [credit]},
            }
            found = resources.extract(parsed)
            cache.enrich(found)
            context = f"{credit_kind}/{row_kind}"

            for row in (row for row in found if row.get("hash")):
                identity = cache.get(row["hash"])
                if identity and identity.get("model_version_id"):
                    assert {key: row.get(key) for key in identity_fields} == {
                        key: identity.get(key) for key in identity_fields
                    }, context

            if row_kind != "no_hash" and credit_kind in {
                "carries_hash",
                "name_only",
            }:
                before = baseline[0]
                after = next(row for row in found if row.get("hash"))
                changed = {
                    key
                    for key in set(before) | set(after)
                    if before.get(key) != after.get(key)
                }
                assert changed <= {"weight", "name_in_prompt"}, context
                assert after["weight"] == credit["weight"], context
                assert after["name_in_prompt"] == credit["modelName"], context
            elif row_kind == "no_hash":
                assert len(found) == 1, context
                assert found[0]["hash"] is None, context
                assert {
                    key: found[0].get(key) for key in identity_fields
                } == {
                    "model_id": credit.get("modelId"),
                    "model_version_id": credit.get("modelVersionId"),
                    "model_name": credit.get("modelName"),
                    "version_name": credit.get("modelVersionName"),
                }, context
                assert found[0]["resolved_from"] == "embedded", context


def test_resource_projection_invariant_across_fixtures_and_override_states(scanned):
    from backend import db
    from backend.metadata import resources
    from backend.store import images as image_store

    fixtures = {
        "with_block": (
            "<lora:BaseLora:0.7>\nSteps: 8, Sampler: Euler, CFG scale: 1, "
            "Model: Base, Model hash: AAAAAAAAAA, "
            'Hashes: {"model":"AAAAAAAAAA","vae":"9999999999",'
            '"lora:BaseLora":"BBBBBBBBBB","embed:BaseEmbed":"CCCCCCCCCC"}, '
            'Lora hashes: "BaseLora: BBBBBBBBBB", '
            'TI hashes: "BaseEmbed: CCCCCCCCCC", '
            'Civitai resources: [{"type":"checkpoint","modelVersionId":101,'
            '"modelName":"Base"},{"type":"lora","modelVersionId":202,'
            '"modelName":"BaseLora","weight":0.7},{"type":"embedding",'
            '"modelVersionId":303,"modelName":"BaseEmbed"}]'
        ),
        "without_block": (
            "<lora:BaseLora:0.7>\nSteps: 8, Sampler: Euler, CFG scale: 1, "
            "Model: Base, Model hash: AAAAAAAAAA, "
            'Hashes: {"model":"AAAAAAAAAA","vae":"9999999999",'
            '"lora:BaseLora":"BBBBBBBBBB","embed:BaseEmbed":"CCCCCCCCCC"}, '
            'Lora hashes: "BaseLora: BBBBBBBBBB", '
            'TI hashes: "BaseEmbed: CCCCCCCCCC"'
        ),
        "hires_upscaler": (
            "<lora:BaseLora:0.7>\nSteps: 8, Sampler: Euler, CFG scale: 1, "
            "Model: Base, Model hash: AAAAAAAAAA, Hires upscaler: 4x_Test, "
            'Hashes: {"model":"AAAAAAAAAA","vae":"9999999999",'
            '"lora:BaseLora":"BBBBBBBBBB","embed:BaseEmbed":"CCCCCCCCCC"}, '
            'Lora hashes: "BaseLora: BBBBBBBBBB", '
            'TI hashes: "BaseEmbed: CCCCCCCCCC"'
        ),
    }
    states = ("no_override", "weight_locked", "checkpoint_locked", "row_added", "row_deleted")
    image_id = scanned[0]

    for fixture_name, raw in fixtures.items():
        original = infotext.parse(raw)
        for state in states:
            with db.transaction() as conn:
                conn.execute("DELETE FROM image_resources WHERE image_id=?", (image_id,))
            image_store.replace_resources(image_id, resources.extract(original))
            for row in image_store.resources_for(image_id):
                identity = {
                    resources.CHECKPOINT: ("A" * 10, 101),
                    resources.LORA: ("B" * 10, 202),
                    resources.EMBEDDING: ("C" * 10, 303),
                    resources.UPSCALER: ("D" * 10, 404),
                }[row["resource_type"]]
                image_store.update_resource(
                    row["id"], {"hash": identity[0], "model_version_id": identity[1]}
                )

            rows = image_store.resources_for(image_id)
            if state == "weight_locked":
                row = next(row for row in rows if row["resource_type"] == resources.LORA)
                image_store.update_resource(row["id"], {"weight": 0.42, "locked_by_user": 1})
            elif state == "checkpoint_locked":
                row = next(
                    row for row in rows if row["resource_type"] == resources.CHECKPOINT
                )
                image_store.update_resource(
                    row["id"],
                    {"hash": "F" * 10, "model_version_id": 111, "locked_by_user": 1},
                )
            elif state == "row_added":
                image_store.add_resource(
                    image_id,
                    {
                        "resource_type": resources.LORA,
                        "name_in_prompt": "AddedLora",
                        "hash": "E" * 10,
                        "weight": 0.25,
                        "model_version_id": 505,
                        "locked_by_user": 1,
                    },
                )
            elif state == "row_deleted":
                row = next(row for row in rows if row["resource_type"] == resources.LORA)
                image_store.update_resource(row["id"], {"deleted_by_user": 1})

            retained = [
                row for row in image_store.resources_for(image_id) if not row["deleted_by_user"]
            ]
            projected_text = resource_edits.project(raw, raw, image_id)
            projected = infotext.parse(projected_text)
            context = f"{fixture_name}/{state}"

            assert projected["prompt"] == original["prompt"], context
            assert projected["negative_prompt"] == original["negative_prompt"], context
            original_plain = [
                (key, value)
                for key, value in original["fields"].items()
                if key not in resource_edits.RESOURCE_FIELDS
            ]
            projected_plain = [
                (key, value)
                for key, value in projected["fields"].items()
                if key not in resource_edits.RESOURCE_FIELDS
            ]
            assert projected_plain == original_plain, context

            actual_tuples = {
                (entry.get("type"), entry["modelVersionId"], entry.get("weight"))
                for entry in projected["fields"].get("Civitai resources", [])
            }
            expected_tuples = {
                (row["resource_type"], row["model_version_id"], row.get("weight"))
                for row in retained
                if row.get("model_version_id") is not None
            }
            assert actual_tuples == expected_tuples, context
            assert all(
                set(entry) <= {"type", "modelVersionId", "weight"}
                for entry in projected["fields"].get("Civitai resources", [])
            ), context

            expected_credits = []
            for row in retained:
                if row.get("model_version_id") is None:
                    continue
                credit = {
                    "type": row["resource_type"],
                    "modelVersionId": row["model_version_id"],
                }
                if row.get("weight") is not None:
                    credit["weight"] = row["weight"]
                expected_credits.append(credit)

            hashes = {"vae": "9999999999"}
            for row in retained:
                if not row.get("hash"):
                    continue
                prefix = {
                    resources.CHECKPOINT: "model",
                    resources.LORA: "lora",
                    resources.EMBEDDING: "embed",
                    resources.UPSCALER: "upscaler",
                }[row["resource_type"]]
                key = "model" if prefix == "model" else f"{prefix}:{row['name_in_prompt']}"
                hashes[key] = row["hash"]
            lora_hashes = {
                row["name_in_prompt"]: row["hash"]
                for row in retained
                if row["resource_type"] == resources.LORA and row.get("hash")
            }
            ti_hashes = {
                row["name_in_prompt"]: row["hash"]
                for row in retained
                if row["resource_type"] == resources.EMBEDDING and row.get("hash")
            }
            checkpoint = next(
                row for row in retained if row["resource_type"] == resources.CHECKPOINT
            )
            expected_fields = {
                "Civitai resources": expected_credits,
                "Hashes": hashes,
                "Model hash": checkpoint["hash"],
                "Lora hashes": lora_hashes,
                "Lora weights": infotext.parse_hash_map(
                    original["fields"].get("Lora weights")
                ),
                "TI hashes": ti_hashes,
            }
            actual_fields = {
                "Civitai resources": projected["fields"].get("Civitai resources"),
                "Hashes": infotext.parse_hash_map(projected["fields"].get("Hashes")),
                "Model hash": projected["fields"].get("Model hash"),
                "Lora hashes": infotext.parse_hash_map(projected["fields"].get("Lora hashes")),
                "Lora weights": infotext.parse_hash_map(
                    projected["fields"].get("Lora weights")
                ),
                "TI hashes": infotext.parse_hash_map(projected["fields"].get("TI hashes")),
            }
            assert actual_fields == expected_fields, context

            original_fields = {
                "Civitai resources": original["fields"].get("Civitai resources"),
                "Hashes": infotext.parse_hash_map(original["fields"].get("Hashes")),
                "Model hash": original["fields"].get("Model hash"),
                "Lora hashes": infotext.parse_hash_map(original["fields"].get("Lora hashes")),
                "Lora weights": infotext.parse_hash_map(
                    original["fields"].get("Lora weights")
                ),
                "TI hashes": infotext.parse_hash_map(original["fields"].get("TI hashes")),
            }
            changed = {
                key
                for key in resource_edits.RESOURCE_FIELDS
                if original_fields[key] != actual_fields[key]
            }
            assert changed == {
                key
                for key in resource_edits.RESOURCE_FIELDS
                if original_fields[key] != expected_fields[key]
            }, context


def test_a1111_fixture_preserves_its_generated_model_and_lora_hashes(client, scanned):
    image_id = _scanned_image_id(
        client, "01a31e2c-78a7-4181-aec5-ef1a78730977_103695029.png"
    )
    raw = client.get(f"/api/images/{image_id}").json()["raw_infotext"]

    projected_text = resource_edits.project(raw, raw, image_id)
    original = infotext.parse(raw)
    projected = infotext.parse(projected_text)

    assert projected_text == raw
    assert projected["prompt"] == original["prompt"]
    assert projected["negative_prompt"] == original["negative_prompt"]
    changed = {
        key
        for key in set(original["fields"]) | set(projected["fields"])
        if original["fields"].get(key) != projected["fields"].get(key)
    }
    assert changed == set()
    assert projected["fields"]["Model hash"] == "d716ef6a78"
    assert projected["fields"]["Lora hashes"] == (
        "MoonTasticDemonStyle: 99b1c11266d7"
    )


def test_projection_is_idempotent_across_both_named_fixtures(client, scanned):
    for filename in (
        "00003-2003483145_139428400.png",
        "01a31e2c-78a7-4181-aec5-ef1a78730977_103695029.png",
    ):
        image_id = _scanned_image_id(client, filename)
        raw = client.get(f"/api/images/{image_id}").json()["raw_infotext"]

        once = resource_edits.project(raw, raw, image_id)
        twice = resource_edits.project(raw, once, image_id)
        three_times = resource_edits.project(raw, twice, image_id)

        assert once == twice == three_times, filename


def test_unlinked_credit_survives_a_prompt_edit(client, scanned):
    from backend.metadata import resources
    from backend.store import images as image_store

    image_id = scanned[0]
    raw = (
        "<lora:SameName:0.5>\nSteps: 8, Sampler: Euler, CFG scale: 1, "
        'Civitai resources: [{"type":"lora","weight":0.9,'
        '"modelVersionId":777,"modelName":"SameName"}]'
    )
    image_store.replace_resources(image_id, resources.extract(infotext.parse(raw)))
    effective = compose.apply_edit(
        raw,
        {
            "draft": {"prompt": "A prompt without the tag", "fields": {}},
            "touched": ["prompt"],
            "deleted": [],
        },
    )

    projected_text = resource_edits.project(raw, effective, image_id)

    assert infotext.parse(projected_text)["fields"]["Civitai resources"] == [
        {
            "type": "lora",
            "weight": 0.9,
            "modelVersionId": 777,
        }
    ]


def test_hash_projection_preserves_existing_fields_without_duplicate_spelling(
    client, scanned
):
    from backend.metadata import resources
    from backend.store import images as image_store

    image_id = scanned[0]
    raw = (
        "<lora:Foo:0.5>\nSteps: 8, Sampler: Euler, CFG scale: 1, Model: Base, "
        "Model hash: 1a2b3c4d5e, "
        'Hashes: {"model": "1a2b3c4d5e", "vae": "9f9f9f9f9f"}, '
        'Lora hashes: "Foo: aaaaaaaaaa"'
    )
    image_store.replace_resources(image_id, resources.extract(infotext.parse(raw)))
    foo = next(
        row for row in image_store.resources_for(image_id) if row["name_in_prompt"] == "Foo"
    )
    image_store.update_resource(foo["id"], {"weight": 0.55, "locked_by_user": 1})

    projected_text = resource_edits.project(raw, raw, image_id)
    projected = infotext.parse(projected_text)
    fields = projected["fields"]

    assert fields["Hashes"] == {
        "model": "1a2b3c4d5e",
        "vae": "9f9f9f9f9f",
        "lora:Foo": "aaaaaaaaaa",
    }
    assert fields["Model hash"] == "1a2b3c4d5e"
    assert fields["Lora hashes"] == "Foo: aaaaaaaaaa"
    document = resource_edits.build_document(raw, raw, image_id)
    meta = upload_document.render_meta(document)
    assert meta is not None
    assert "resources" not in meta


def test_credit_without_a_version_id_is_not_projected(client, scanned):
    from backend.metadata import resources
    from backend.store import images as image_store

    image_id = scanned[0]
    raw = (
        "<lora:Other:0.5>\nSteps: 8, Sampler: Euler, CFG scale: 1, "
        'Civitai resources: [{"type":"lora","weight":0.9,'
        '"modelName":"NoIdHere"}]'
    )
    image_store.replace_resources(image_id, resources.extract(infotext.parse(raw)))
    no_id = next(
        row for row in image_store.resources_for(image_id) if row["name_in_prompt"] == "NoIdHere"
    )
    image_store.update_resource(no_id["id"], {"weight": 0.55, "locked_by_user": 1})

    projected_text = resource_edits.project(raw, raw, image_id)
    fields = infotext.parse(projected_text)["fields"]

    assert "Civitai resources" not in fields


def test_resolving_an_idless_credit_replaces_it_once(client, scanned):
    from backend.metadata import resources
    from backend.store import images as image_store

    image_id = scanned[0]
    raw = (
        "<lora:Alpha:0.5>, <lora:NoId:0.9>\n"
        "Steps: 8, Sampler: Euler, CFG scale: 1, "
        'Civitai resources: [{"type":"lora","weight":0.9,"modelName":"NoId"}]'
    )
    image_store.replace_resources(image_id, resources.extract(infotext.parse(raw)))
    no_id = next(
        row for row in image_store.resources_for(image_id) if row["name_in_prompt"] == "NoId"
    )
    attached = client.post(
        f"/api/resources/{no_id['id']}/attach",
        json={
            "model": {"id": 4200, "name": "Resolved NoId"},
            "version": {"id": 4242, "name": "v1", "hash_autov2": "ABCDEF1234"},
        },
    )
    assert attached.status_code == 200

    fields = infotext.parse(resource_edits.project(raw, raw, image_id))["fields"]
    credits = fields["Civitai resources"]

    assert len(credits) == 1
    assert credits[0] == {"type": "lora", "modelVersionId": 4242, "weight": 0.9}
    assert all(key not in fields for key in ("Hashes", "Model hash", "Lora hashes", "TI hashes"))


def test_a_weight_lock_does_not_invent_a_hashes_field(client, scanned):
    from backend.store import images as image_store

    image_id = _scanned_image_id(
        client, "01a31e2c-78a7-4181-aec5-ef1a78730977_103695029.png"
    )
    raw = client.get(f"/api/images/{image_id}").json()["raw_infotext"]
    resource = next(
        row
        for row in image_store.resources_for(image_id)
        if row["resource_type"] == "lora"
    )
    image_store.update_resource(resource["id"], {"weight": 0.42, "locked_by_user": 1})

    original = infotext.parse(raw)
    projected = infotext.parse(resource_edits.project(raw, raw, image_id))
    changed = {
        key
        for key in set(original["fields"]) | set(projected["fields"])
        if original["fields"].get(key) != projected["fields"].get(key)
    }

    assert "Hashes" not in projected["fields"]
    assert changed == set()


def test_bulk_preview_compares_two_projected_documents(client, scanned):
    from backend.store import images as image_store

    image_id = _scanned_image_id(client, "00003-2003483145_139428400.png")
    resource = next(
        row
        for row in image_store.resources_for(image_id)
        if row["resource_type"] == "lora"
    )
    image_store.update_resource(resource["id"], {"weight": 0.42, "locked_by_user": 1})

    response = client.post(
        "/api/images/edit/bulk",
        json={
            "image_ids": [image_id],
            "operations": {"fields": {"Steps": 25}},
            "apply": False,
        },
    )

    assert response.status_code == 200
    assert [change["field"] for change in response.json()["plan"][0]["changes"]] == [
        "Steps"
    ]


_PROMPT_RESOURCE_RAW = (
    "<lora:KeepLora:0.7>, <lora:KeepEmbedding:0.6>, <lora:DeleteMe:0.5>, scene\n"
    "Steps: 8, Sampler: Euler, CFG scale: 1, Model: Test model, "
    'Hashes: {"lora:KeepLora":"AAAAAAAAAA","embed:KeepEmbedding":"BBBBBBBBBB",'
    '"lora:DeleteMe":"CCCCCCCCCC"}, '
    'Lora hashes: "KeepLora: AAAAAAAAAA, DeleteMe: CCCCCCCCCC", '
    'TI hashes: "KeepEmbedding: BBBBBBBBBB", '
    'Civitai resources: [{"type":"lora","weight":0.7,"hash":"AAAAAAAAAA",'
    '"modelVersionId":101,"modelName":"KeepLora"},{"type":"embedding",'
    '"weight":0.6,"hash":"BBBBBBBBBB","modelVersionId":202,'
    '"modelName":"KeepEmbedding"},{"type":"lora","weight":0.5,'
    '"hash":"CCCCCCCCCC","modelVersionId":303,"modelName":"DeleteMe"}]'
)


def _install_prompt_resource_fixture(image_id: int) -> None:
    from backend import db
    from backend.metadata import resources
    from backend.store import images as image_store

    parsed = infotext.parse(_PROMPT_RESOURCE_RAW)
    derived, prompt_links = resources.extract_with_prompt_links(parsed)
    assert {
        (resources.LORA, "KeepLora"),
        (resources.EMBEDDING, "KeepEmbedding"),
    } <= prompt_links
    with db.transaction() as conn:
        conn.execute(
            "UPDATE images SET raw_infotext=? WHERE id=?",
            (_PROMPT_RESOURCE_RAW, image_id),
        )
    image_store.replace_resources(image_id, derived)
    rows = image_store.resources_for(image_id)
    keep = next(row for row in rows if row["name_in_prompt"] == "KeepLora")
    deleted = next(row for row in rows if row["name_in_prompt"] == "DeleteMe")
    image_store.update_resource(
        keep["id"], {"weight": 0.42, "locked_by_user": 1}
    )
    image_store.update_resource(deleted["id"], {"deleted_by_user": 1})
    image_store.add_resource(
        image_id,
        {
            "resource_type": resources.LORA,
            "name_in_prompt": "AddedLora",
            "hash": "DDDDDDDDDD",
            "weight": 0.25,
            "model_version_id": 404,
            "locked_by_user": 1,
        },
    )


def _assert_prompt_resource_upload(image_id: int, expected_prompt: str) -> None:
    from backend.metadata import resources
    from backend.posts import materialise
    from backend.store import images as image_store

    image = image_store.get(image_id) or {}
    projected = infotext.parse(materialise.upload_infotext(image))
    meta = materialise.upload_document(image)
    expected_credits = {
        (resources.LORA, 101, 0.42),
        (resources.EMBEDDING, 202, 0.6),
        (resources.LORA, 404, 0.25),
    }
    block_credits = {
        (entry["type"], entry["modelVersionId"], entry.get("weight"))
        for entry in projected["fields"]["Civitai resources"]
    }
    assert meta is not None
    meta_credits = {
        (entry["type"], entry["modelVersionId"], entry.get("weight"))
        for entry in meta["civitaiResources"]
    }

    assert projected["prompt"] == expected_prompt
    assert meta["prompt"] == expected_prompt
    assert block_credits == meta_credits == expected_credits
    assert infotext.parse_hash_map(projected["fields"]["Lora hashes"]) == {
        "KeepLora": "AAAAAAAAAA",
        "AddedLora": "DDDDDDDDDD",
    }
    assert infotext.parse_hash_map(projected["fields"]["TI hashes"]) == {
        "KeepEmbedding": "BBBBBBBBBB"
    }
    assert projected["fields"]["Hashes"] == {
        "lora:KeepLora": "AAAAAAAAAA",
        "embed:KeepEmbedding": "BBBBBBBBBB",
        "lora:AddedLora": "DDDDDDDDDD",
    }


def test_local_prompt_edit_retains_resource_rows_and_overrides(client, scanned):
    from backend.metadata import resources
    from backend.store import images as image_store

    image_id = scanned[0]
    _install_prompt_resource_fixture(image_id)

    response = client.put(
        f"/api/images/{image_id}/edit",
        json={
            "draft": {"prompt": "rewritten scene without tags", "fields": {}},
            "touched": ["prompt"],
            "deleted": [],
        },
    )

    assert response.status_code == 200
    _assert_prompt_resource_upload(image_id, "rewritten scene without tags")

    image_store.replace_resources(
        image_id, resources.extract(infotext.parse(_PROMPT_RESOURCE_RAW))
    )
    reopened = client.get(f"/api/images/{image_id}/edit").json()
    rows = {row["name_in_prompt"]: row for row in reopened["resources"]}
    assert rows["KeepLora"]["weight"] == 0.42
    assert rows["KeepLora"]["locked_by_user"] == 1
    assert rows["AddedLora"]["added_by_user"] == 1
    assert rows["DeleteMe"]["deleted_by_user"] == 1
    _assert_prompt_resource_upload(image_id, "rewritten scene without tags")


def test_bulk_prompt_edit_retains_prompt_linked_resources(client, scanned):
    image_id = scanned[0]
    _install_prompt_resource_fixture(image_id)

    response = client.post(
        "/api/images/edit/bulk",
        json={
            "image_ids": [image_id],
            "operations": {
                "find_replace": {
                    "target": "prompt",
                    "find": r"<lora:[^>]+>,?\s*",
                    "replace": "",
                    "regex": True,
                }
            },
            "apply": True,
        },
    )

    assert response.status_code == 200
    assert response.json()["changed"] == 1
    _assert_prompt_resource_upload(image_id, "scene")


def test_derived_fields_are_marked_stripped_and_refused(client, scanned):
    from backend.store import images as image_store

    image_id = scanned[0]
    state = client.get(f"/api/images/{image_id}/edit").json()
    civitai = next(
        field for field in state["diff"]["fields"] if field["key"] == "Civitai resources"
    )
    assert civitai["derived"] is True

    response = client.put(
        f"/api/images/{image_id}/edit",
        json={
            "draft": {"fields": {"Civitai resources": []}},
            "touched": ["Civitai resources"],
            "deleted": [],
        },
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "derived_metadata_field"

    normal = editing.normalise(
        image_store.get(image_id),
        {"fields": {"Steps": 17, "Hashes": {"lora:x": "ABC"}}},
        ["Steps", "Hashes"],
        ["Model hash"],
    )
    assert normal["touched"] == ["Steps"]
    assert normal["deleted"] == []
    assert normal["draft"]["fields"] == {"Steps": 17}


def test_duplicate_hashes_project_once_and_do_not_warn(client, scanned):
    image_id = scanned[0]
    raw = (
        "<lora:One:0.7>\nSteps: 8, Sampler: Euler, CFG scale: 1, "
        'Lora hashes: "One: AAAAAAAAAA, One: AAAAAAAAAA"'
    )

    projected = infotext.parse(resource_edits.project(raw, raw, image_id))

    assert projected["fields"]["Lora hashes"] == "One: AAAAAAAAAA"
    assert linter.lint(projected) == []


def test_upload_exclusions_match_fields_and_the_drawer_shows_the_upload(client, scanned):
    from backend import db
    from backend.metadata import resources
    from backend.posts import materialise
    from backend.store import images as image_store

    image = next(
        image_store.get(image_id)
        for image_id in scanned
        if "Hires upscale" in (image_store.get(image_id) or {}).get("raw_infotext", "")
    )
    original = infotext.parse(image["raw_infotext"])
    upscaler = next(
        row
        for row in image_store.resources_for(image["id"])
        if row["resource_type"] == resources.UPSCALER
    )
    image_store.update_resource(upscaler["id"], {"model_version_id": 164821})
    db.set_json_setting(materialise.EXCLUDED_FIELDS_SETTING, ["Sampler", "Hires *"])

    upload_text = materialise.upload_infotext(image)
    upload = infotext.parse(upload_text)
    document = materialise.upload_document(image)

    assert "Sampler" not in upload["fields"]
    assert not any(key.startswith("Hires ") for key in upload["fields"])
    assert not any(
        entry.get("type") == resources.UPSCALER
        for entry in upload["fields"].get("Civitai resources", [])
    )
    assert document is not None
    assert not any(
        entry.get("type") == resources.UPSCALER
        for entry in document.get("civitaiResources", [])
    )
    assert "Steps" in upload["fields"]
    assert upload["prompt"] == original["prompt"]
    assert upload["negative_prompt"] == original["negative_prompt"]
    assert client.get(f"/api/images/{image['id']}/edit").json()["effective_infotext"] == (
        upload_text
    )
    image_store.update_resource(upscaler["id"], {"model_version_id": None})
    plan = editing.bulk_plan([image], {"fields": {"Steps": 17}})
    assert [change["field"] for change in plan[0]["changes"]] == ["Steps"]


def test_comfyui_drawer_uses_the_library_path_for_workflow_replacement_and_undo(client, tmp_path):
    from backend import jobs
    from backend.metadata import png_io
    from backend.posts import materialise
    from backend.scanner import service as scanner
    from backend.store import images as image_store

    raw = json.dumps(
        {
            "Positive": {"class_type": "CLIPTextEncode", "inputs": {"text": "scene"}},
            "Sampler": {
                "class_type": "KSampler",
                "inputs": {
                    "positive": ["Positive", 0],
                    "seed": 7000001,
                    "steps": 20,
                    "cfg": 5,
                },
            },
        }
    )
    workflow = '{"nodes":[{"id":"Sampler","widgets_values":[7000001]}]}'
    folder = tmp_path / "comfyui-drawer"
    folder.mkdir()
    source = folder / "drawer.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", raw)
    info.add_text("workflow", workflow)
    Image.new("RGB", (64, 64)).save(source, "PNG", pnginfo=info)
    added = client.post("/api/sources", json={"path": str(folder), "label": "ComfyUI drawer"})
    assert added.status_code == 200
    scanner.scan_roots(jobs.Job(id=0, kind="scan"))
    image = image_store.get_by_path(str(source))
    assert image is not None and "source_path" not in image

    edited = client.put(
        f"/api/images/{image['id']}/edit",
        json={"draft": {"fields": {"Seed": 8000002}}, "touched": ["Seed"], "deleted": []},
    )
    assert edited.status_code == 200
    assert edited.json()["comfyui_workflow_replaced"] is True
    post_image = {**image_store.get(image["id"]), "source_path": str(source)}
    changed = materialise.path_for(post_image, tmp_path, materialise.upload_infotext(post_image))
    changed_facts = png_io.read(changed)
    assert changed_facts is not None and "prompt" not in changed_facts.chunks

    undone = client.put(
        f"/api/images/{image['id']}/edit",
        json={"draft": {"fields": {}}, "touched": [], "deleted": []},
    )
    assert undone.status_code == 200
    assert undone.json()["comfyui_workflow_replaced"] is False
    restored = materialise.path_for(post_image, tmp_path, materialise.upload_infotext(post_image))
    restored_facts = png_io.read(restored)
    assert restored_facts is not None
    assert restored_facts.chunks["prompt"] == raw
    assert restored_facts.chunks["workflow"] == workflow


def test_schema_ten_strips_derived_fields_and_deletes_empty_rows(scanned):
    from backend import db

    edits.save(
        scanned[0],
        draft={"fields": {"Civitai resources": []}},
        touched=["Civitai resources"],
        deleted=[],
    )
    edits.save(
        scanned[1],
        draft={"prompt": "kept", "fields": {"Hashes": {"lora:x": "ABC"}}},
        touched=["prompt", "Hashes"],
        deleted=["TI hashes"],
    )
    conn = db.get_connection()
    conn.execute("PRAGMA user_version=9")
    conn.commit()

    db.init_db()

    assert edits.get(scanned[0]) is None
    surviving = edits.get(scanned[1])
    assert surviving is not None
    assert surviving["touched"] == ["prompt"]
    assert surviving["deleted"] == []
    assert surviving["draft"]["fields"] == {}
    assert edits.count() == 1


def test_the_completion_list_offers_the_fields_these_images_carry(client, scanned):
    """Scoped to the edit, not to the whole library.

    A completion list that offers a field none of the affected images has would
    be as misleading as no list at all.
    """
    post = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()

    library = client.get("/api/settings/metadata-fields").json()["items"]
    scoped = client.get(
        "/api/settings/metadata-fields", params={"post_id": post["id"]}
    ).json()["items"]

    carried = set(
        client.get(f"/api/images/{scanned[0]}/edit").json()["effective"]["fields"]
    )
    assert carried, "the fixture image carries no fields at all"
    offered = {item["field_name"] for item in scoped}
    assert offered <= carried, f"offered a field the image does not carry: {offered - carried}"
    assert offered & carried, "offered nothing the image carries"
    assert len(scoped) <= len(library)


def test_a_delete_field_that_matches_nothing_is_reported_not_applied(client, scanned):
    """The maintainer typed `version` for `Version` and got an empty preview."""
    real = next(
        name
        for name in client.get(f"/api/images/{scanned[0]}/edit").json()["effective"]["fields"]
        if name.lower() != name
    )

    typo = client.post(
        "/api/images/edit/bulk",
        json={
            "image_ids": [scanned[0]],
            "operations": {"delete_fields": [real.lower()]},
            "apply": False,
        },
    ).json()
    assert typo["unmatched_delete_fields"] == [real.lower()]
    assert not any(item["changes"] for item in typo["plan"])

    spelled = client.post(
        "/api/images/edit/bulk",
        json={
            "image_ids": [scanned[0]],
            "operations": {"delete_fields": [real]},
            "apply": False,
        },
    ).json()
    assert spelled["unmatched_delete_fields"] == []
    assert any(item["changes"] for item in spelled["plan"])
