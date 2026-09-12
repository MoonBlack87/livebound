"""The bounded local-model hashing path and its bulk CivitAI lookup."""

# The embedded real infotext intentionally retains its three original long lines.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time

import pytest
from PIL import Image, PngImagePlugin

from backend import db, jobs, watchers
from backend.civitai import client as civitai_client
from backend.civitai import rest
from backend.civitai.errors import AuthError, RateLimited
from backend.metadata import infotext, resource_edits, resources
from backend.resources import cache, model_hashing
from backend.scanner import service as image_scanner
from backend.store import images as image_store
from backend.store import model_roots, sources

REAL_UPSCALER_INFOTEXT = """masterpiece, best quality, very aesthetic, absurdres, ultra high textures, razor sharp execution, mobe-godeen, boneswm, cat made of bones, made in Skulls for the Skull Throne ((in Skulls style)), BREAK, (riding a skull toilet throne), eating pizza, pizza, pizza eating, action pose, action composition, dynamic composition, dynamic movement, wings, giant wings, flying, hovering, ((sunglasses, wearing mirror sunglasses)), oversized hat, top hat, cute surface, big glossy eyes, open mouth, joyful scream, seductive, provocative, motion blur, basecap, ((world made out of Skulls balls, Skulls balls, made of Skulls)), hanging skulls, skulls everywhere, massive skulls balls, glossy editorial style, crisp detail, sharp focus, vivid, luscious, (no humans:1.15), warped expressions, caricature grimace, unhinged excitement, ardent, fervent, jumping, flying, ((mountain background)), hyper detailed background, colorful, vibrant, rainbow, pixar-like chaos style, pixar movie like, 3d animation, fast movement, action shot, motion blur, dutch angle, BREAK, In a whimsical world made entirely of skulls, a cat made of bones sits atop a giant skull toilet throne, wearing oversized mirror sunglasses and a top hat. Its big, glossy eyes are wide with joyful excitement as it eats a slice of pizza, its open mouth a caricature grimace of unhinged delight. The cat's wings, a giant pair of colorful, vibrant wings, are spread wide, as if it's about to take flight, while its basecap is adorned with a skull-shaped charm. In the background, a mountain range made of skulls stretches out, with a rainbow-colored sky and a sense of Pixar-like chaos. The cat's motion is blurred, as if it's jumping and flying at the same time, its seductive and provocative pose frozen in time., high textures, lazypos, lazypos, mobe-fl0rid3, pixar, <lora:FloatingAndRiding-SDXL_epoch_9:0.75>, <lora:SkullsfortheSkullThrone:0.70>, <lora:Bones_World_Morph:0.45>
Negative prompt: bad quality, worst quality, low quality, lowres, jpeg artifacts, bad anatomy, bad hands, simple background, white background, black background, grey background, lazyneg
Steps: 20, Sampler: Euler a, Schedule type: Karras, CFG scale: 5.5, Seed: 1801438905, Size: 896x1216, Model hash: d716ef6a78, Model: MoonArtCauldronMix_XL_V1.0.0, Denoising strength: 0.45, Clip skip: 2, ENSD: 31337, Prompt Enhancer: placeholder, Hires prompt: "masterpiece, best quality, very aesthetic, absurdres, ultra high textures, razor sharp execution, mobe-godeen, boneswm, cat made of bones, made in Skulls for the Skull Throne ((in Skulls style)), BREAK, (riding a skull toilet throne), eating pizza, pizza, pizza eating, action pose, action composition, dynamic composition, dynamic movement, wings, giant wings, flying, hovering, ((sunglasses, wearing mirror sunglasses)), oversized hat, top hat, , cute surface, big glossy eyes, open mouth, joyful scream, seductive, provocative, motion blur, basecap,  ((world made out of Skulls balls, Skulls balls, made of Skulls)), hanging skulls, skulls everywhere, massive skulls balls,glossy editorial style, crisp detail, sharp focus, vivid, luscious, (no humans:1.15), warped expressions, caricature grimace, unhinged excitement, ardent, fervent, jumping, flying, ((mountain background)), hyper detailed background, colorful, vibrant, rainbow, pixar-like chaos style, pixar movie like, 3d animation, fast movement, action shot, motion blur, dutch angle, BREAK, CAPTION_HERE, high textures, lazypos, <lora:FloatingAndRiding-SDXL_epoch_9:0.75>, lazypos, mobe-fl0rid3, pixar, <lora:SkullsfortheSkullThrone:0.70>, <lora:Bones_World_Morph:0.45>,", Hires CFG Scale: 5.5, Hires upscale: 1.25, Hires steps: 10, Hires upscaler: 4x_foolhardy_Remacri, Lora hashes: "FloatingAndRiding-SDXL_epoch_9: acf36adf9a6f, SkullsfortheSkullThrone: 743348a3277a, Bones_World_Morph: f1abb61d3082", TI hashes: "lazypos: 30866692653c, lazypos: 30866692653c, lazyneg: ba21023c7054, lazyneg: ba21023c7054", Eta: 0.67"""


def test_unchanged_model_files_are_skipped_and_changed_files_are_rehashed(
    tmp_path, monkeypatch
):
    folder = tmp_path / "models"
    folder.mkdir()
    small = folder / "small.safetensors"
    large = folder / "large.ckpt"
    small.write_bytes(b"small")
    large.write_bytes(b"larger model")
    root = model_roots.add_root(str(folder))

    real_hash = model_hashing._hash_and_read_names
    calls = []

    def tracked_hash(path, file_size, should_stop=None):
        calls.append(path.name)
        return real_hash(path, file_size, should_stop)

    monkeypatch.setattr(model_hashing, "_hash_and_read_names", tracked_hash)
    monkeypatch.setattr(
        model_hashing.cache,
        "resolve_hashes_bulk",
        lambda hashes: {"resolved": 0, "unknown": len(hashes), "cached": 0, "failed": 0},
    )

    first = model_hashing.hash_roots(jobs.Job(id=1, kind="model-hash"))
    second = model_hashing.hash_roots(jobs.Job(id=2, kind="model-hash"))
    previous_mtime = large.stat().st_mtime
    large.write_bytes(b"changed file")
    os.utime(large, (previous_mtime + 1, previous_mtime + 1))
    third = model_hashing.hash_roots(jobs.Job(id=3, kind="model-hash"))

    assert calls == ["small.safetensors", "large.ckpt", "large.ckpt"]
    assert first["hashed"] == 2
    assert second["hashed"] == 0 and second["skipped"] == 2
    assert third["hashed"] == 1 and third["skipped"] == 1

    db.get_connection().execute(
        "UPDATE model_files SET file_stem='' WHERE absolute_path=?", (str(small),)
    )
    db.get_connection().commit()
    read_names = model_hashing._read_names

    def broken_header(path, file_size):
        if path == small:
            raise OSError("model disappeared")
        return read_names(path, file_size)

    monkeypatch.setattr(model_hashing, "_read_names", broken_header)
    fourth = model_hashing.hash_roots(jobs.Job(id=4, kind="model-hash"))

    assert fourth["failed"] == 1 and fourth["skipped"] == 1
    assert fourth["hashed"] == 0 and fourth["resolved"] == 0
    assert model_roots.get_file(root["id"], small) is not None


def test_a_vanished_model_loses_its_path_but_keeps_its_identity(
    client, tmp_path, monkeypatch
):
    folder = tmp_path / "models"
    folder.mkdir()
    path = folder / "remember-me.safetensors"
    path.write_bytes(b"model bytes")
    root = model_roots.add_root(str(folder))
    monkeypatch.setattr(
        model_hashing.cache,
        "resolve_hashes_bulk",
        lambda hashes: {"resolved": 0, "unknown": len(hashes), "cached": 0, "failed": 0},
    )

    model_hashing.hash_roots(jobs.Job(id=1, kind="model-hash"))
    file_id = model_roots.get_file(root["id"], path)["id"]
    digest = hashlib.sha256(path.read_bytes()).hexdigest().upper()
    cache.put(
        digest,
        {
            "id": 123,
            "modelId": 12,
            "name": "v1",
            "model": {"name": "Remembered model", "type": "LoRA"},
        },
        source="rest",
        status=200,
    )

    path.unlink()
    result = model_hashing.hash_roots(jobs.Job(id=2, kind="model-hash"))

    assert result["forgotten"] == 1
    assert model_roots.get_file_by_id(file_id) is None
    resource = db.get_connection().execute(
        "SELECT * FROM resource_map WHERE hash=?", (digest,)
    ).fetchone()
    assert resource["model_version_id"] == 123

    inventory = client.get("/api/model-files").json()
    remembered = next(row for row in inventory["items"] if row["sha256"] == digest)
    assert remembered["absolute_path"] is None
    assert remembered["locally_available"] is False
    assert remembered["model_name"] == "Remembered model"
    assert client.get("/api/model-roots").json()["recognized"] == 1


def test_prompt_tags_find_stem_and_safetensors_output_names(tmp_path, monkeypatch):
    folder = tmp_path / "models"
    folder.mkdir()
    stem_file = folder / "GrandlineLSIv1.0Krea.safetensors"
    output_file = folder / "otherwise-named.safetensors"
    stem_file.write_bytes(_safetensors({}))
    output_file.write_bytes(
        _safetensors(
            {
                "ss_output_name": "zy_Inflatable_World_Morph _v1",
                "modelspec.title": "Inflatable World Morph",
            }
        )
    )
    model_roots.add_root(str(folder))
    monkeypatch.setattr(
        model_hashing.cache,
        "resolve_hashes_bulk",
        lambda hashes: {"resolved": 0, "unknown": len(hashes), "cached": 0, "failed": 0},
    )

    model_hashing.hash_roots(jobs.Job(id=1, kind="model-hash"))
    stem_hash = hashlib.sha256(stem_file.read_bytes()).hexdigest().upper()
    output_hash = hashlib.sha256(output_file.read_bytes()).hexdigest().upper()

    assert model_roots.hash_for_prompt_tag("grandline lsi v1 0 krea") == stem_hash
    assert model_roots.hash_for_prompt_tag("ZY inflatable world morph v1") == output_hash
    assert model_roots.hash_for_prompt_tag("not installed") is None

    cache.put(
        output_hash,
        {"id": 123, "modelId": 12, "name": "v1", "model": {"name": "World", "type": "LoRA"}},
        source="manual",
        status=200,
    )
    found = resources.extract(
        infotext.parse(
            "<lora:zy_Inflatable_World_Morph _v1:0.4>\n"
            "Steps: 8, Sampler: Euler, CFG scale: 1, "
            'Civitai resources: [{"type":"lora","weight":0.65,'
            '"modelVersionId":123,"modelName":"Inflatable World"}]'
        )
    )
    assert [(row["name_in_prompt"], row["hash"], row["weight"]) for row in found] == [
        ("zy_Inflatable_World_Morph _v1", output_hash, 0.4)
    ]


def _store_inventory_file(
    folder,
    name,
    digest,
    *,
    size=100,
    ss_output_name=None,
    modelspec_title=None,
):
    root = model_roots.add_root(str(folder))
    model_roots.upsert_file(
        root["id"],
        folder / name,
        name,
        mtime=1,
        size=size,
        sha256=digest,
        file_stem=(folder / name).stem,
        ss_output_name=ss_output_name,
        modelspec_title=modelspec_title,
    )


def test_hires_upscaler_uses_the_local_inventory_for_its_credit(scanned, tmp_path):
    digest = "E1A73BD89C2DA1AE494774746398689048B5A892BD9653E146713F9DF8BCA86A"
    folder = tmp_path / "models"
    folder.mkdir()
    _store_inventory_file(folder, "4x_foolhardy_Remacri.pth", digest)
    cache.assign_manual(digest, 147759, 164821)

    parsed = infotext.parse(REAL_UPSCALER_INFOTEXT)
    found = resources.extract(parsed)
    cache.enrich(found)
    upscaler = next(row for row in found if row["resource_type"] == resources.UPSCALER)
    assert upscaler["name_in_prompt"] == "4x_foolhardy_Remacri"
    assert upscaler["hash"] == digest
    assert upscaler["model_version_id"] == 164821

    image_id = scanned[0]
    image_store.replace_resources(image_id, found)
    projected = infotext.parse(
        resource_edits.project(REAL_UPSCALER_INFOTEXT, REAL_UPSCALER_INFOTEXT, image_id)
    )
    assert {"type": "upscaler", "modelVersionId": 164821} in projected["fields"][
        "Civitai resources"
    ]

    with db.transaction() as conn:
        conn.execute("DELETE FROM model_files")
    unknown = resources.extract(parsed)
    cache.enrich(unknown)
    image_store.replace_resources(image_id, unknown)
    projected_unknown = infotext.parse(
        resource_edits.project(REAL_UPSCALER_INFOTEXT, REAL_UPSCALER_INFOTEXT, image_id)
    )
    assert not any(
        entry.get("type") == "upscaler"
        for entry in projected_unknown["fields"].get("Civitai resources", [])
    )


def test_schema_sixteen_reextracts_upscalers_and_keeps_resource_overrides(
    scanned, tmp_path
):
    digest = "E1A73BD89C2DA1AE494774746398689048B5A892BD9653E146713F9DF8BCA86A"
    folder = tmp_path / "migration-models"
    folder.mkdir()
    _store_inventory_file(folder, "4x_foolhardy_Remacri.pth", digest)
    cache.assign_manual(digest, 147759, 164821)
    raw = (
        "<lora:Keep:0.5>, <lora:Delete:0.4>\n"
        "Steps: 8, Sampler: Euler, CFG scale: 1, Model: Base, "
        "Hires upscaler: 4x_foolhardy_Remacri, "
        'Lora hashes: "Keep: AAAAAAAAAA, Delete: BBBBBBBBBB"'
    )
    image_id = scanned[0]
    existing = [
        row
        for row in resources.extract(infotext.parse(raw))
        if row["resource_type"] != resources.UPSCALER
    ]
    image_store.replace_resources(image_id, existing)
    image_store.add_resource(
        image_id,
        {
            "resource_type": "lora",
            "name_in_prompt": "Added",
            "model_version_id": 333,
        },
    )
    conn = db.get_connection()
    conn.execute("UPDATE images SET raw_infotext=? WHERE id=?", (raw, image_id))
    conn.execute(
        "UPDATE image_resources SET locked_by_user=1, model_version_id=111 "
        "WHERE image_id=? AND name_in_prompt='Keep'",
        (image_id,),
    )
    conn.execute(
        "UPDATE image_resources SET deleted_by_user=1 "
        "WHERE image_id=? AND name_in_prompt='Delete'",
        (image_id,),
    )
    conn.execute("PRAGMA user_version=15")
    conn.commit()

    db.init_db()

    migrated = {
        (row["resource_type"], row["name_in_prompt"]): row
        for row in image_store.resources_for(image_id)
    }
    upscaler = migrated[(resources.UPSCALER, "4x_foolhardy_Remacri")]
    assert upscaler["model_version_id"] == 164821
    assert migrated[(resources.LORA, "Keep")]["locked_by_user"] == 1
    assert migrated[(resources.LORA, "Keep")]["model_version_id"] == 111
    assert migrated[(resources.LORA, "Delete")]["deleted_by_user"] == 1
    assert migrated[(resources.LORA, "Added")]["added_by_user"] == 1
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_model_duplicate_report_is_separate_bounded_and_path_complete(client, tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    _store_inventory_file(first, "model.safetensors", "A" * 64, size=400)
    _store_inventory_file(second, "copy.safetensors", "A" * 64, size=400)
    _store_inventory_file(first, "other.safetensors", "B" * 64, size=200)
    _store_inventory_file(second, "other-copy.safetensors", "B" * 64, size=200)

    inventory = client.get("/api/model-files").json()
    first_page = client.get("/api/model-file-duplicates?limit=1").json()
    second_page = client.get("/api/model-file-duplicates?limit=1&offset=1").json()

    assert "duplicate_groups" not in inventory
    assert first_page["total"] == 2
    assert first_page["redundant_size"] == 600
    assert len(first_page["groups"]) == len(second_page["groups"]) == 1
    assert first_page["groups"][0]["redundant_size"] == 400
    assert {path["root_path"] for path in first_page["groups"][0]["paths"]} == {
        str(first),
        str(second),
    }
    assert second_page["groups"][0]["sha256"] == "B" * 64


def test_model_inventory_reports_the_names_used_for_matching(tmp_path):
    folder = tmp_path / "models"
    folder.mkdir()
    _store_inventory_file(
        folder,
        "otherwise-named.safetensors",
        "B" * 64,
        ss_output_name="training-output",
        modelspec_title="Display title",
    )

    rows, total = model_roots.inventory(query="training-output")

    assert total == 1
    assert rows[0]["identities"] == [
        {"source": "file_stem", "name": "otherwise-named"},
        {"source": "ss_output_name", "name": "training-output"},
        {"source": "modelspec_title", "name": "Display title"},
    ]


def test_model_inventory_counts_and_filters_each_identity_state(client, tmp_path):
    folder = tmp_path / "models"
    folder.mkdir()
    known_hash = "C" * 64
    unrecognized_hash = "D" * 64
    unqueried_hash = "E" * 64
    _store_inventory_file(folder, "known.safetensors", known_hash)
    _store_inventory_file(folder, "unrecognized.safetensors", unrecognized_hash)
    _store_inventory_file(folder, "unqueried.safetensors", unqueried_hash)
    cache.put(
        known_hash,
        {"id": 123, "modelId": 12, "name": "v1", "model": {"name": "Known"}},
        source="rest",
        status=200,
    )
    cache.put(unrecognized_hash, None, source="rest", status=404)

    counts = client.get("/api/model-roots").json()
    filtered = {
        name: client.get("/api/model-files", params={"filter_name": name}).json()
        for name in ("recognized", "unrecognized", "unqueried")
    }

    assert {name: counts[name] for name in filtered} == {
        "recognized": 1,
        "unrecognized": 1,
        "unqueried": 1,
    }
    assert {
        name: (report["total"], [row["file_stem"] for row in report["items"]])
        for name, report in filtered.items()
    } == {
        "recognized": (1, ["known"]),
        "unrecognized": (1, ["unrecognized"]),
        "unqueried": (1, ["unqueried"]),
    }


def test_bulk_model_lookup_never_sends_more_than_one_hundred_hashes(monkeypatch):
    batch_sizes = []

    def fake_post_json(url, payload, **kwargs):
        batch_sizes.append(len(payload))
        return []

    monkeypatch.setattr(rest.client, "post_json", fake_post_json)
    rest.model_versions_by_hash([f"{value:064X}" for value in range(205)])

    assert batch_sizes == [100, 100, 5]


def test_bulk_miss_is_recorded_and_not_asked_again(monkeypatch):
    file_hash = "A" * 64
    calls = []

    def no_matches(hashes):
        calls.append(hashes)
        return {}

    monkeypatch.setattr(rest, "model_versions_by_hash", no_matches)
    first = cache.resolve_hashes_bulk([file_hash])
    second = cache.resolve_hashes_bulk([file_hash])
    row = db.get_connection().execute(
        "SELECT http_status FROM resource_map WHERE hash=?", (file_hash,)
    ).fetchone()

    assert first["unknown"] == 1
    assert second["cached"] == 1
    assert calls == [[file_hash]]
    assert row["http_status"] == 404


@pytest.mark.parametrize(
    ("payload", "expected_version", "expected_status", "result_key", "reapplied"),
    [
        (
            {
                "id": 4242,
                "modelId": 42,
                "name": "AddNet version",
                "model": {"name": "AddNet model", "type": "LoRA"},
            },
            4242,
            200,
            "image_resolved",
            1,
        ),
        (None, None, 404, "image_unidentified", 0),
    ],
    ids=["resolved", "remembered-miss"],
)
def test_hash_models_resolves_an_unmatched_image_hash(
    scanned,
    monkeypatch,
    payload,
    expected_version,
    expected_status,
    result_key,
    reapplied,
):
    addnet_hash = "ACF36ADF9A6F"
    with db.transaction() as conn:
        conn.execute("DELETE FROM image_resources")
    image_store.replace_resources(
        scanned[0],
        [
            {
                "resource_type": "lora",
                "name_in_prompt": "Renamed LoRA",
                "hash": addnet_hash,
            }
        ],
    )
    requests = []

    def resolve(file_hash):
        requests.append(file_hash)
        return payload

    monkeypatch.setattr(rest, "model_version_by_hash", resolve)

    result = model_hashing.hash_roots(
        jobs.Job(id=1, kind="model-hash"), user_requested=True
    )
    resource = image_store.resources_for(scanned[0])[0]
    remembered = cache.get_exact(addnet_hash)

    assert requests == [addnet_hash]
    assert result[result_key] == 1
    assert result["image_failed"] == 0
    assert result["reapplied"] == reapplied
    assert resource["model_version_id"] == expected_version
    assert remembered is not None
    assert remembered["http_status"] == expected_status


def test_hash_models_does_not_ask_about_or_touch_protected_resources(
    scanned, monkeypatch
):
    with db.transaction() as conn:
        conn.execute("DELETE FROM image_resources")
    locked = image_store.add_resource(
        scanned[0],
        {
            "resource_type": "lora",
            "name_in_prompt": "Locked",
            "hash": "A" * 12,
            "locked_by_user": 1,
        },
    )
    manual = image_store.add_resource(
        scanned[0],
        {
            "resource_type": "lora",
            "name_in_prompt": "Manual",
            "hash": "B" * 12,
            "resolved_from": "manual",
        },
    )
    before = {
        locked["id"]: image_store.resource(locked["id"]),
        manual["id"]: image_store.resource(manual["id"]),
    }
    requests = []
    monkeypatch.setattr(rest, "model_version_by_hash", lambda value: requests.append(value))

    result = model_hashing.hash_roots(
        jobs.Job(id=1, kind="model-hash"), user_requested=True
    )

    assert requests == []
    assert result["image_resolved"] == result["image_unidentified"] == 0
    assert {
        locked["id"]: image_store.resource(locked["id"]),
        manual["id"]: image_store.resource(manual["id"]),
    } == before


@pytest.mark.parametrize(
    ("refusal", "expected_reason", "retry_after", "next_step"),
    [
        (RateLimited("request limit reached", retry_after=37), "RateLimited", 37, "later"),
        (AuthError("connection refused"), "AuthError", None, "reconnect CivitAI"),
    ],
    ids=["rate-limited", "authentication-refused"],
)
def test_hash_models_stops_refused_image_lookups_until_the_next_run(
    scanned, monkeypatch, refusal, expected_reason, retry_after, next_step
):
    hashes = ["A" * 12, "B" * 12, "C" * 12, "D" * 12]
    with db.transaction() as conn:
        conn.execute("DELETE FROM image_resources")
    image_store.replace_resources(
        scanned[0],
        [
            {
                "resource_type": "lora",
                "name_in_prompt": f"Resource {index}",
                "hash": file_hash,
            }
            for index, file_hash in enumerate(hashes)
        ],
    )
    resolved = {
        "id": 4242,
        "modelId": 42,
        "name": "Known version",
        "model": {"name": "Known model", "type": "LoRA"},
    }
    first_requests = []

    def refuse_second(file_hash):
        first_requests.append(file_hash)
        if len(first_requests) == 2:
            raise refusal
        return resolved

    monkeypatch.setattr(rest, "model_version_by_hash", refuse_second)
    job = jobs.Job(id=1, kind="model-hash")

    result = model_hashing.hash_roots(job, user_requested=True)

    assert first_requests == hashes[:2]
    assert result["image_resolved"] == 1
    assert result["image_failed"] == 0
    assert result["image_lookup_stopped_early"] is True
    assert result["image_lookup_remaining"] == 3
    assert result["image_lookup_resumes_next_run"] is True
    if retry_after is None:
        assert "image_lookup_retry_after" not in result
    else:
        assert result["image_lookup_retry_after"] == retry_after
    assert result["reapplied"] == 1
    assert image_store.resources_for(scanned[0])[0]["model_version_id"] == 4242
    assert job.stage == "Done"
    assert len(job.items) == 1
    assert job.items[0]["status"] == "error"
    assert expected_reason in job.items[0]["message"]
    assert next_step in job.items[0]["message"]
    assert "3 image-resource hashes remain open" in job.items[0]["message"]
    assert cache.get_exact(hashes[0])["http_status"] == 200
    assert all(cache.get_exact(file_hash) is None for file_hash in hashes[1:])

    second_requests = []
    monkeypatch.setattr(
        rest,
        "model_version_by_hash",
        lambda file_hash: second_requests.append(file_hash),
    )

    next_result = model_hashing.hash_roots(
        jobs.Job(id=2, kind="model-hash"), user_requested=True
    )

    assert second_requests == hashes[1:]
    assert next_result["image_unidentified"] == 3


def test_watched_model_root_does_not_resolve_open_image_resources(
    scanned, tmp_path, monkeypatch
):
    model_folder = tmp_path / "watched-models"
    model_folder.mkdir()
    model_path = model_folder / "new.ckpt"
    model_path.write_bytes(b"new model")
    root = model_roots.add_root(str(model_folder))
    model_roots.set_watch_enabled(root["id"], True)
    image_hash = "A" * 12
    with db.transaction() as conn:
        conn.execute("DELETE FROM image_resources")
    image_store.replace_resources(
        scanned[0],
        [{"resource_type": "lora", "name_in_prompt": "Open", "hash": image_hash}],
    )
    model_requests = []
    image_requests = []
    monkeypatch.setattr(
        rest,
        "model_versions_by_hash",
        lambda hashes: model_requests.append(hashes) or {},
    )
    monkeypatch.setattr(
        rest,
        "model_version_by_hash",
        lambda file_hash: image_requests.append(file_hash),
    )

    started = watchers.check_model_roots()
    job = _wait_for_job(started)

    digest = hashlib.sha256(model_path.read_bytes()).hexdigest().upper()
    assert job.status == "done"
    assert model_requests == [[digest]]
    assert image_requests == []
    assert model_roots.get_file(root["id"], model_path)["sha256"] == digest
    assert "reapplied" in job.result
    assert cache.get_exact(image_hash) is None


def test_explicit_hash_models_resolves_open_image_resources(
    client, scanned, monkeypatch
):
    image_hash = "B" * 12
    with db.transaction() as conn:
        conn.execute("DELETE FROM image_resources")
    image_store.replace_resources(
        scanned[0],
        [{"resource_type": "lora", "name_in_prompt": "Open", "hash": image_hash}],
    )
    image_requests = []
    monkeypatch.setattr(
        rest,
        "model_version_by_hash",
        lambda file_hash: image_requests.append(file_hash),
    )

    response = client.post("/api/model-roots/hash")
    assert response.status_code == 200
    job = _wait_for_job(response.json())

    assert job.status == "done"
    assert image_requests == [image_hash]
    assert job.result["image_unidentified"] == 1


def _wait_for_job(started: dict) -> jobs.Job:
    job_id = started.get("job", started.get("id"))
    assert job_id is not None, f"no job was started: {started}"
    for _ in range(500):
        job = jobs.get(job_id)
        if job is not None and job.status not in ("starting", "running"):
            return job
        time.sleep(0.01)
    raise AssertionError(f"job {job_id} did not finish")


def test_resolve_for_images_counts_only_hashes_whose_lookup_was_stored(
    scanned, monkeypatch
):
    partial_hash = "A" * 10
    full_hash = "B" * 64
    payload = {
        "id": 123,
        "modelId": 12,
        "name": "Automatic version",
        "model": {"name": "Automatic model", "type": "LoRA"},
    }
    image_store.replace_resources(
        scanned[0],
        [
            {
                "resource_type": "lora",
                "name_in_prompt": name,
                "hash": file_hash,
            }
            for name, file_hash in (("manual-race", partial_hash), ("stored", full_hash))
        ],
    )

    monkeypatch.setattr(
        rest,
        "model_versions_by_hash",
        lambda hashes: dict.fromkeys(hashes, payload),
    )

    def single_lookup(file_hash):
        cache.assign_manual(file_hash, 42, 4242)
        return payload

    monkeypatch.setattr(rest, "model_version_by_hash", single_lookup)

    result = cache.resolve_for_images([scanned[0]])
    rows = {
        row["hash"]: dict(row)
        for row in db.get_connection().execute(
            "SELECT * FROM resource_map WHERE hash IN (?, ?)",
            (partial_hash, full_hash),
        )
    }

    assert result == {
        "resolved": 1,
        "unknown": 0,
        "cached": 0,
        "failed": 0,
        "reapplied": 2,
    }
    assert rows[partial_hash]["source"] == "manual"
    assert rows[partial_hash]["model_version_id"] == 4242
    assert rows[full_hash]["source"] == "rest"
    assert rows[full_hash]["model_version_id"] == 123


def test_cache_prefers_each_exact_hash_when_prefixes_overlap():
    short_hash = "BA21023C7054"
    full_hash = short_hash + "D4488F77" + "A" * 44
    short_payload = {
        "id": 1860747,
        "modelId": 1,
        "name": "lazyneg v3",
        "model": {"name": "lazyneg", "type": "TextualInversion"},
    }
    full_payload = {
        "id": 2121199,
        "modelId": 1,
        "name": "lazyneg",
        "model": {"name": "lazyneg", "type": "TextualInversion"},
    }
    cache.put(short_hash, short_payload, source="metadata-app", status=200)
    cache.put(full_hash, full_payload, source="rest", status=200)

    assert cache.get(short_hash)["model_version_id"] == 1860747
    assert cache.get(full_hash)["model_version_id"] == 2121199


def test_manual_model_assignment_supersedes_prefix_but_preserves_user_resource(
    monkeypatch, scanned
):
    short_hash = "E" * 10
    file_hash = short_hash + "F" * 54
    automatic = {
        "id": 111,
        "modelId": 11,
        "name": "Automatic version",
        "model": {"name": "Automatic model", "type": "LoRA"},
    }
    cache.put(short_hash, automatic, source="rest", status=200)
    cache.put(file_hash, automatic, source="rest", status=200)
    resource = image_store.add_resource(
        scanned[0],
        {
            "resource_type": "lora",
            "name_in_prompt": "test-model",
            "hash": short_hash,
            "model_id": 11,
            "model_version_id": 111,
        },
    )

    assignment = cache.assign_manual(file_hash, 42, 4242)
    reapplied = cache.reapply_hash(file_hash)

    def unexpected_lookup(_hashes):
        pytest.fail("an automatic lookup must not touch a manual assignment")

    monkeypatch.setattr(rest, "model_versions_by_hash", unexpected_lookup)

    result = cache.resolve_hashes_bulk([short_hash], force=True)
    row = cache.get(short_hash)
    image_resource = image_store.resource(resource["id"])
    prefix_rows = db.get_connection().execute(
        "SELECT hash FROM resource_map WHERE hash_prefix=?", (short_hash,)
    ).fetchall()

    assert result["cached"] == 1
    assert assignment["source"] == row["source"] == "manual"
    assert (row["model_id"], row["model_version_id"]) == (42, 4242)
    assert row["http_status"] is None and row["raw_json"] is None
    assert [stored["hash"] for stored in prefix_rows] == [file_hash]
    assert reapplied == 0
    assert (image_resource["model_id"], image_resource["model_version_id"]) == (
        11,
        111,
    )


def test_model_url_with_version_yields_both_ids():
    assert cache.parse_model_url(
        "https://anything.invalid/models/456/name?modelVersionId=789"
    ) == (456, 789)


def test_model_url_without_version_is_refused():
    with pytest.raises(cache.ModelVersionRequired, match="version"):
        cache.parse_model_url("https://civitai.com/models/456/name")


def test_image_scanner_does_not_walk_a_model_root(tmp_path):
    source_folder = tmp_path / "images"
    model_folder = tmp_path / "models"
    source_folder.mkdir()
    model_folder.mkdir()
    source_image = source_folder / "source.png"
    model_image = model_folder / "model-root-bait.png"
    Image.new("RGB", (8, 8), color="navy").save(source_image)
    shutil.copy2(source_image, model_image)
    sources.add_root(str(source_folder))
    model_roots.add_root(str(model_folder))

    result = image_scanner.scan_roots(jobs.Job(id=1, kind="scan"))
    rows = db.get_connection().execute("SELECT absolute_path FROM images").fetchall()

    assert result["scanned"] == 1
    assert [row["absolute_path"] for row in rows] == [str(source_image)]


def test_image_scan_never_queries_civitai(fixture_images, monkeypatch):
    folder, _ = fixture_images
    requests = []

    def unexpected_request(*args, **kwargs):
        requests.append((args, kwargs))
        return None

    monkeypatch.setattr(civitai_client, "get_json", unexpected_request)
    monkeypatch.setattr(civitai_client, "post_json", unexpected_request)
    sources.add_root(str(folder))

    result = image_scanner.scan_roots(jobs.Job(id=1, kind="scan"))

    assert result["scanned"] == 2
    assert requests == []


@pytest.mark.parametrize("order", ["images-then-models", "models-then-images"])
def test_scans_reapply_every_unlocked_cached_image_resource(
    tmp_path, monkeypatch, order
):
    """Either completed scan projects every known prefix, including old rows."""
    model_folder = tmp_path / "models"
    model_folder.mkdir()
    model_path = model_folder / "known.safetensors"
    model_path.write_bytes(_safetensors({}))
    known_hash = hashlib.sha256(model_path.read_bytes()).hexdigest().upper()
    known_prefix = known_hash[:10]
    unknown_prefix = "F" * 10

    image_folder = tmp_path / "images"
    image_folder.mkdir()
    image_path = image_folder / "resources.png"
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text(
        "parameters",
        "a resource test\n"
        "Negative prompt: none\n"
        "Steps: 1, Sampler: Euler, CFG scale: 1, Seed: 1, Size: 12x12, "
        f"Model hash: {known_prefix}, Model: Known checkpoint, "
        f'Lora hashes: "Known lora: {known_prefix}, Unknown lora: {unknown_prefix}"',
    )
    Image.new("RGB", (12, 12), color="navy").save(image_path, "PNG", pnginfo=metadata)
    sources.add_root(str(image_folder))
    model_roots.add_root(str(model_folder))

    identity = {
        "id": 4242,
        "modelId": 42,
        "name": "Known version",
        "model": {"name": "Known model", "type": "LoRA"},
    }

    def resolve_known_hashes(hashes):
        for file_hash in hashes:
            cache.put(file_hash, identity, source="rest", status=200)
        return {"resolved": len(hashes), "unknown": 0, "cached": 0, "failed": 0}

    monkeypatch.setattr(cache, "resolve_hashes_bulk", resolve_known_hashes)

    def scan_images():
        return image_scanner.scan_roots(jobs.Job(id=1, kind="scan"))

    def scan_models():
        return model_hashing.hash_roots(jobs.Job(id=2, kind="model-hash"))

    snapshots: dict[str, dict] = {}

    def lock_checkpoint_and_snapshot() -> None:
        with db.transaction() as conn:
            conn.execute(
                """
                UPDATE image_resources SET
                    locked_by_user=1, model_id=NULL, model_version_id=NULL,
                    model_name='Locked model', version_name='Locked version',
                    resolved_from='manual'
                WHERE resource_type='checkpoint'
                """
            )
        rows = {
            row["name_in_prompt"]: dict(row)
            for row in db.get_connection().execute("SELECT * FROM image_resources")
        }
        snapshots["locked"] = rows["Known checkpoint"]
        snapshots["unknown"] = rows["Unknown lora"]

    if order == "images-then-models":
        scan_images()
        lock_checkpoint_and_snapshot()
        result = scan_models()
    else:
        scan_models()
        # The terminal reconciliation, rather than ingest-time enrichment, is
        # what clears a row that was already stored before this image pass.
        monkeypatch.setattr(image_scanner.resource_cache, "enrich", lambda _: None)
        real_reapply = cache.reapply_unresolved

        def lock_before_reapply():
            lock_checkpoint_and_snapshot()
            return real_reapply()

        monkeypatch.setattr(cache, "reapply_unresolved", lock_before_reapply)
        result = scan_images()

    rows = {
        row["name_in_prompt"]: dict(row)
        for row in db.get_connection().execute("SELECT * FROM image_resources")
    }
    assert result["reapplied"] == 1
    assert rows["Known checkpoint"] == snapshots["locked"]
    assert rows["Unknown lora"] == snapshots["unknown"]

    for row in rows.values():
        cached = cache.get(row["hash_prefix"])
        if row["locked_by_user"] or not cached or cached["model_version_id"] is None:
            continue
        assert (
            row["model_id"],
            row["model_version_id"],
            row["model_name"],
            row["version_name"],
            row["resolved_from"],
        ) == (
            cached["model_id"],
            cached["model_version_id"],
            cached["model_name"],
            cached["version_name"],
            "resource_map",
        )


def test_an_added_row_with_nothing_in_it_is_not_a_decision(scanned, tmp_path):
    """Adding a row says the user created it, not that they chose its identity.

    A locked row and a deleted row are decisions and stay untouched. So does a
    half-made one, a model picked without a version yet, which carries
    `resolved_from='manual'` rather than a `model_version_id`; that case is
    covered by `test_resolve_step_uses_only_unambiguous_local_file_stems`. An
    added row holding nothing at all protects nothing, and leaving it unresolved
    only sent the user through Find model for a model already on their disk.
    """
    folder = tmp_path / "models"
    folder.mkdir()
    for name, digest, version_id in (
        ("Added", "d" * 64, 771),
        ("Locked", "e" * 64, 772),
        ("Removed", "f" * 64, 773),
    ):
        _store_inventory_file(folder, f"{name}.safetensors", digest)
        cache.put(
            digest,
            {
                "id": version_id,
                "modelId": version_id + 1000,
                "name": f"Version {version_id}",
                "model": {"name": f"Model {version_id}", "type": "LoRA"},
            },
            source="rest",
            status=200,
        )

    # Through the production writer, not raw SQL. An earlier version of this
    # test manufactured its own `unresolved` row and stayed green while the
    # reported defect was untouched, because `add_resource` stamped every added
    # row `manual` and the projector refused exactly that.
    added = image_store.add_resource(scanned[0], {"resource_type": "lora", "name_in_prompt": "Added"})["id"]
    locked = image_store.add_resource(
        scanned[0], {"resource_type": "lora", "name_in_prompt": "Locked", "locked_by_user": 1}
    )["id"]
    removed = image_store.add_resource(scanned[0], {"resource_type": "lora", "name_in_prompt": "Removed"})["id"]
    with db.transaction() as conn:
        conn.execute("UPDATE image_resources SET deleted_by_user=1 WHERE id=?", (removed,))

    assert image_store.resource(added)["resolved_from"] == "unresolved", (
        "a row added with only a name has no identity to mark as manual"
    )

    cache.reapply_unresolved()

    resolved = {
        int(r["id"]): r["model_version_id"]
        for r in db.get_connection().execute(
            "SELECT id, model_version_id FROM image_resources WHERE id IN (?,?,?)",
            (added, locked, removed),
        )
    }
    assert resolved[added] == 771, "an added row holding nothing is resolvable"
    assert resolved[locked] is None, "a locked row is a decision"
    assert resolved[removed] is None, "a deleted row is a decision"


def test_the_same_file_twice_is_one_candidate_but_two_files_are_none(scanned, tmp_path):
    """Ambiguity is about identities, not about rows in the inventory.

    A backup copy of a model beside the original is the commonest shape a model
    folder takes, and counting file rows made every one of those names
    unresolvable. Two *different* files under one stem stay ambiguous, because
    then there really is no way to tell which one a prompt meant.
    """
    same = tmp_path / "same"
    backup = tmp_path / "same-backup"
    other = tmp_path / "other"
    for folder in (same, backup, other):
        folder.mkdir()

    shared_digest = "a" * 64
    _store_inventory_file(same, "Copied.safetensors", shared_digest)
    _store_inventory_file(backup, "Copied.safetensors", shared_digest)
    _store_inventory_file(other, "Twinned.safetensors", "b" * 64)
    _store_inventory_file(same, "Twinned.safetensors", "c" * 64)
    # "c" * 64 is deliberately left without an identity. The asymmetric case is
    # the one the old comment named: a stem where only one of two different
    # files is recognised must stay ambiguous, or an implementation that counts
    # only recognised hashes resolves it to the wrong file and passes anyway.
    for digest, version_id in ((shared_digest, 501), ("b" * 64, 502)):
        cache.put(
            digest,
            {
                "id": version_id,
                "modelId": version_id * 10,
                "name": f"Version {version_id}",
                "model": {"name": f"Model {version_id}", "type": "LoRA"},
            },
            source="rest",
            status=200,
        )

    def unresolved(name: str) -> int:
        with db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO image_resources(image_id, resource_type, name_in_prompt,"
                " hash, hash_prefix, resolved_from) VALUES(?,?,?,?,?,?)",
                (scanned[0], "lora", name, None, "", "unresolved"),
            )
        return int(cursor.lastrowid)

    copied = unresolved("Copied")
    twinned = unresolved("Twinned")

    cache.reapply_unresolved()

    rows = {
        int(row["id"]): dict(row)
        for row in db.get_connection().execute(
            "SELECT id, model_version_id, resolved_from FROM image_resources WHERE id IN (?, ?)",
            (copied, twinned),
        )
    }
    assert rows[copied]["model_version_id"] == 501
    assert rows[copied]["resolved_from"] == "local_name"
    assert rows[twinned]["model_version_id"] is None, (
        "two different files stay ambiguous even when only one is recognised"
    )


def test_local_name_resolution_uses_the_safetensors_output_name(scanned, tmp_path):
    folder = tmp_path / "models"
    folder.mkdir()
    file_hash = "5C529B811AA6" + "1" * 52
    output_name = "aidmaMJ6.1_v0.5_2"
    _store_inventory_file(
        folder,
        "aidmaMJ6.1SDXL-v0.5.safetensors",
        file_hash,
        ss_output_name=output_name,
    )
    cache.put(
        file_hash,
        {
            "id": 5205,
            "modelId": 5206,
            "name": "v0.5",
            "model": {"name": "Aidma MJ6.1", "type": "LoRA"},
        },
        source="rest",
        status=200,
    )
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO image_resources(image_id, resource_type, name_in_prompt, hash,"
            " hash_prefix, resolved_from) VALUES(?,?,?,?,?,?)",
            (scanned[0], "lora", output_name, "de37bbdde0af", "DE37BBDDE0", "unresolved"),
        )

    cache.reapply_unresolved()

    row = image_store.resource(int(cursor.lastrowid))
    assert row["model_version_id"] == 5205
    assert row["resolved_from"] == "local_name"


def test_local_name_resolution_is_ambiguous_across_file_name_columns(scanned, tmp_path):
    folder = tmp_path / "models"
    folder.mkdir()
    stem_hash = "A" * 64
    output_hash = "B" * 64
    _store_inventory_file(folder, "Shared Name.safetensors", stem_hash)
    _store_inventory_file(
        folder,
        "Different.safetensors",
        output_hash,
        ss_output_name="shared_name",
    )
    for digest, version_id in ((stem_hash, 6101), (output_hash, 6102)):
        cache.put(
            digest,
            {
                "id": version_id,
                "modelId": version_id + 100,
                "name": f"Version {version_id}",
                "model": {"name": f"Model {version_id}", "type": "LoRA"},
            },
            source="rest",
            status=200,
        )
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO image_resources(image_id, resource_type, name_in_prompt, hash,"
            " hash_prefix, resolved_from) VALUES(?,?,?,?,?,?)",
            (scanned[0], "lora", "SHARED-NAME", None, "", "unresolved"),
        )

    cache.reapply_unresolved()

    row = image_store.resource(int(cursor.lastrowid))
    assert row["model_version_id"] is None
    assert row["resolved_from"] == "unresolved"


def test_hash_identity_replaces_local_name_but_local_name_never_replaces_hash(
    scanned, tmp_path
):
    folder = tmp_path / "models"
    folder.mkdir()
    local_hash = "1" * 64
    _store_inventory_file(folder, "Shared name.safetensors", local_hash)
    cache.put(
        local_hash,
        {
            "id": 101,
            "modelId": 1001,
            "name": "Name-derived version",
            "model": {"name": "Name-derived model", "type": "LoRA"},
        },
        source="rest",
        status=200,
    )

    def resource(name, hash_value, *, version_id=None, resolved_from="unresolved"):
        with db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO image_resources(image_id, resource_type, name_in_prompt, hash,"
                " hash_prefix, model_id, model_version_id, model_name, version_name,"
                " resolved_from) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    scanned[0],
                    "lora",
                    name,
                    hash_value,
                    hash_value[:10],
                    version_id + 1000 if version_id else None,
                    version_id,
                    f"Model {version_id}" if version_id else None,
                    f"Version {version_id}" if version_id else None,
                    resolved_from,
                ),
            )
        return int(cursor.lastrowid)

    later_hash = "B" * 12
    name_first = resource("shared NAME", later_hash)
    hash_first = resource("Shared name", "C" * 12, version_id=303, resolved_from="resource_map")

    assert cache.reapply_unresolved() == 1
    assert (image_store.resource(name_first)["model_version_id"], image_store.resource(name_first)[
        "resolved_from"
    ]) == (101, "local_name")

    cache.put(
        later_hash,
        {
            "id": 202,
            "modelId": 2002,
            "name": "Hash-derived version",
            "model": {"name": "Hash-derived model", "type": "LoRA"},
        },
        source="rest",
        status=200,
    )

    assert cache.reapply_unresolved() == 1
    assert (image_store.resource(name_first)["model_version_id"], image_store.resource(name_first)[
        "resolved_from"
    ]) == (202, "resource_map")
    assert (image_store.resource(hash_first)["model_version_id"], image_store.resource(hash_first)[
        "resolved_from"
    ]) == (303, "resource_map")


def test_resolve_step_uses_only_unambiguous_local_file_stems(scanned, tmp_path):
    folder = tmp_path / "models"
    folder.mkdir()

    def local_file(name, digest, version_id):
        _store_inventory_file(folder, name, digest)
        cache.put(
            digest,
            {
                "id": version_id,
                "modelId": version_id + 1000,
                "name": f"Version {version_id}",
                "model": {"name": f"Model {version_id}", "type": "LoRA"},
            },
            source="rest",
            status=200,
        )

    local_file("Named LoRA.safetensors", "1" * 64, 101)
    local_file("4x_ExactUpscaler.pth", "2" * 64, 102)
    local_file("Ambiguous.safetensors", "3" * 64, 103)
    local_file("Ambiguous.ckpt", "4" * 64, 104)
    local_file("Hash wins.safetensors", "5" * 64, 105)
    for flag, digest, version_id in (
        ("locked_by_user", "6" * 64, 106),
        ("added_by_user", "7" * 64, 107),
        ("deleted_by_user", "8" * 64, 108),
    ):
        local_file(f"{flag}.safetensors", digest, version_id)

    cache.put(
        "A" * 10,
        {
            "id": 999,
            "modelId": 1999,
            "name": "Hash version",
            "model": {"name": "Hash model", "type": "LoRA"},
        },
        source="rest",
        status=200,
    )

    def resource(name, *, hash_value=None, resource_type="lora", **flags):
        with db.transaction() as conn:
            cursor = conn.execute(
                "INSERT INTO image_resources(image_id, resource_type, name_in_prompt, hash,"
                " hash_prefix, model_id, model_version_id, model_name, version_name,"
                " resolved_from, locked_by_user, added_by_user, deleted_by_user)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    scanned[0],
                    resource_type,
                    name,
                    hash_value,
                    (hash_value or "")[:10],
                    77 if flags else None,
                    None,
                    "Protected model" if flags else None,
                    "Protected version" if flags else None,
                    "manual" if flags else "unresolved",
                    int(bool(flags.get("locked_by_user"))),
                    int(bool(flags.get("added_by_user"))),
                    int(bool(flags.get("deleted_by_user"))),
                ),
            )
        return int(cursor.lastrowid)

    named_lora = resource("named lora", hash_value="B" * 12)
    upscaler = resource("4X_EXACTUPSCALER", resource_type="upscaler")
    ambiguous = resource("ambiguous")
    hash_wins = resource("Hash wins", hash_value="A" * 10)
    protected = {
        flag: resource(flag, hash_value="A" * 10, **{flag: True})
        for flag in ("locked_by_user", "added_by_user", "deleted_by_user")
    }

    assert cache.reapply_hash("A" * 10) == 1
    assert cache.reapply_unresolved() == 2

    assert (image_store.resource(named_lora)["model_version_id"], image_store.resource(named_lora)[
        "resolved_from"
    ]) == (101, "local_name")
    assert (image_store.resource(upscaler)["model_version_id"], image_store.resource(upscaler)[
        "resolved_from"
    ]) == (102, "local_name")
    assert image_store.resource(ambiguous)["model_version_id"] is None
    assert (image_store.resource(hash_wins)["model_version_id"], image_store.resource(hash_wins)[
        "resolved_from"
    ]) == (999, "resource_map")
    for resource_id in protected.values():
        row = image_store.resource(resource_id)
        assert (row["model_id"], row["model_version_id"], row["resolved_from"]) == (
            77,
            None,
            "manual",
        )


def _safetensors(metadata: dict[str, str]) -> bytes:
    header = json.dumps({"__metadata__": metadata}, separators=(",", ":")).encode()
    header += b" " * (-len(header) % 8)
    return len(header).to_bytes(8, "little") + header


def test_a_suggestion_carries_the_hash_a_name_alone_cannot(scanned, tmp_path):
    """Both sources answer, and a model known twice over is offered once.

    Typing a name is a guess; a hash is an identity. The list exists so the
    second is what actually gets recorded. It has to reach a model that was
    assigned through Find model and never downloaded - that one has no file to
    match a name against, so without the map half of this it would be
    unreachable by name for ever.
    """
    folder = tmp_path / "suggested"
    folder.mkdir()
    _store_inventory_file(folder, "Twilight Detail.safetensors", "a" * 64)
    _store_inventory_file(folder, "Twilight Sharpen.safetensors", "b" * 64)
    # The same model, on disk and in the map. One entry, not two.
    cache.put(
        "a" * 64,
        {
            "id": 4711,
            "modelId": 42,
            "name": "v2",
            "model": {"name": "Twilight Detail XL", "type": "LoRA"},
        },
        source="rest",
        status=200,
    )
    # Never downloaded: no file carries this hash.
    cache.put(
        "c" * 64,
        {
            "id": 4712,
            "modelId": 43,
            "name": "v1",
            "model": {"name": "Twilight Bloom", "type": "LoRA"},
        },
        source="rest",
        status=200,
    )

    found = {item["name"]: item for item in model_roots.suggest_resources("twilight")}

    assert set(found) == {"Twilight Detail", "Twilight Sharpen", "Twilight Bloom"}, (
        "a model that exists as a file and as an identity is one suggestion, not two"
    )
    assert found["Twilight Detail"]["hash"] == "A" * 64
    assert found["Twilight Detail"]["model_version_id"] == 4711
    assert found["Twilight Sharpen"]["model_version_id"] is None, (
        "a local file with no identity is still offerable; the hash is what it contributes"
    )
    assert found["Twilight Bloom"]["hash"] == "C" * 64, (
        "a model that was never downloaded is reachable by name through the map"
    )

    assert model_roots.suggest_resources("") == [], "an empty query is not a wildcard"


def test_an_underscore_in_a_model_name_is_a_character_not_a_wildcard(tmp_path):
    """Half the model files on disk have one, and LIKE reads it as "any character"."""
    folder = tmp_path / "underscored"
    folder.mkdir()
    _store_inventory_file(folder, "4x_foolhardy_Remacri.pth", "1" * 64)
    _store_inventory_file(folder, "4xAfoolhardyBRemacri.pth", "2" * 64)

    names = {item["name"] for item in model_roots.suggest_resources("4x_foolhardy")}

    assert names == {"4x_foolhardy_Remacri"}


def test_a_resource_added_from_a_suggestion_is_resolved_at_once(client, scanned, tmp_path):
    """Picking a suggestion records the identity, not a spelling to look up later.

    The hash the suggestion carries is already in the map, so the credit exists
    the moment the row is written. Going through a resolve run for an answer we
    are holding would cost a request and leave the image unattributed until it
    came back.
    """
    folder = tmp_path / "picked"
    folder.mkdir()
    _store_inventory_file(folder, "Chosen.safetensors", "9" * 64)
    cache.put(
        "9" * 64,
        {
            "id": 5150,
            "modelId": 99,
            "name": "v3",
            "model": {"name": "Chosen One", "type": "LoRA"},
        },
        source="rest",
        status=200,
    )

    response = client.post(
        f"/api/images/{scanned[0]}/resources",
        json={"resource_type": "lora", "name": "Chosen", "hash": "9" * 64},
    )
    assert response.status_code == 200, response.text
    row = response.json()
    assert row["model_version_id"] == 5150
    assert row["model_name"] == "Chosen One"
    assert row["resolved_from"] == "resource_map"
    assert row["added_by_user"] == 1
    assert row["hash"] == "9" * 64


def test_a_suggestion_hash_only_copies_its_exact_identity(client, scanned):
    mapped = "A" * 10 + "1" * 54
    selected = "A" * 10 + "2" * 54
    cache.put(
        mapped,
        {
            "id": 6160,
            "modelId": 100,
            "name": "v1",
            "model": {"name": "Different model", "type": "LoRA"},
        },
        source="rest",
        status=200,
    )

    response = client.post(
        f"/api/images/{scanned[0]}/resources",
        json={"resource_type": "lora", "name": "Unmapped", "hash": selected},
    )

    assert response.status_code == 200, response.text
    row = response.json()
    assert row["hash"] == selected
    assert row["model_id"] is None
    assert row["model_version_id"] is None
    assert row["resolved_from"] == "unresolved"


def test_an_exact_suggestion_precedes_more_than_the_limit_of_partial_matches(tmp_path):
    folder = tmp_path / "ranked-suggestions"
    folder.mkdir()
    query = "Finished model name"
    _store_inventory_file(folder, f"{query}.safetensors", "F" * 64)
    for index in range(6):
        _store_inventory_file(
            folder,
            f"{query} variation {index}.safetensors",
            f"{index + 1:X}" * 64,
        )

    found = model_roots.suggest_resources(query, limit=3)

    assert found[0]["name"] == query
    assert len(found) == 3
