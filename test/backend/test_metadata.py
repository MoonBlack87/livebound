"""Reading A1111 infotext, against the real generated files.

The parameter line cannot be split on commas: `Civitai resources` holds a JSON
array and `Lora hashes` holds a quoted string full of them. Both fixtures below
contain exactly those traps.
"""

from __future__ import annotations

from collections import Counter

from backend.metadata import display, infotext, png_io
from backend.metadata import resources as resource_extract

CLOSE_PNG_INFOTEXT = (
    "Textured anime portrait of a pale young woman with a short ink-blue bob, luminous "
    "orange eyes, and a distant melancholy expression. White and vermilion flowers grow "
    "through the right side of her hair while one slender hand rests against her cheek. "
    "Cream paper, fine navy linework, and scattered coral paint marks frame the close "
    "frontal face., <lora:GrandlineLSIv1.0Krea:0.75>, "
    "<lora:AfterveilWornMemoryStylev1.0Krea:0.70>,\n"
    "Steps: 8, Sampler: Euler, Schedule type: Simple, CFG scale: 1.0, Seed: 4160194983, "
    "Size: 925x1248, Model: krea2_turbo_fp8_scaled, Module 1: qwen_image_vae, Module 2: "
    "qwen3vl_4b_fp8_scaled, Denoising strength: 0.4, RNG: GPU, Hires Module 1: Use same "
    "choices, Hires upscale: 1.35, Hires upscaler: 4x_foolhardy_Remacri, Hires steps: 4, "
    "Hires CFG Scale: 1.0, Version: neo-2.27, Civitai resources: "
    '[{"type":"checkpoint","modelVersionId":3072332,"modelName":"Krea 2 Turbo",'
    '"modelVersionName":"v1.0"},{"type":"lora","weight":0.75,'
    '"modelVersionId":3161188,"modelName":"\\ud83d\\udc3b Grandline - Legendary Stylized '
    'Illustration","modelVersionName":"Krea2v1.0"},{"type":"lora","weight":0.7,'
    '"modelVersionId":3197671,"modelName":"\\ud83d\\udc3b Afterveil - Worn Memory Style",'
    '"modelVersionName":"v1.0Krea"}]'
)

REFORGE_INFOTEXT = (
    "<lora:text:0.2>, <lora:textxl:0.4>, "
    "<lora:zy_Inflatable_World_Morph _v1:1.0>, "
    "<lora:MoonTasticCuteCreatures:0.6>, <lora:T0m4t0M0rph:0.8>\n"
    "Steps: 20, Sampler: Euler a, CFG scale: 5.5, "
    'Lora hashes: "text: 111111111111, textxl: 222222222222, '
    "zy_Inflatable_World_Morph _v1: 333333333333, "
    'MoonTasticCuteCreatures: 444444444444, T0m4t0M0rph: 555555555555", '
    'TI hashes: "lazypos: 666666666666, lazyneg: 777777777777"'
)


def test_the_parameter_line_survives_embedded_json(fixture_images):
    _, images = fixture_images
    for path in images:
        facts = png_io.read(path)
        parsed = infotext.parse(facts.infotext)
        assert parsed["fields"], f"{path.name}: no fields read"
        assert not parsed["warnings"], f"{path.name}: {parsed['warnings']}"


def test_a_civitai_resources_array_is_read_as_json(fixture_images):
    _, images = fixture_images
    for path in images:
        parsed = infotext.parse(png_io.read(path).infotext)
        value = parsed["fields"].get("Civitai resources")
        if value is not None:
            assert isinstance(value, list), "has to arrive as a list, not as text"
            assert all(isinstance(item, dict) for item in value)
            return
    # neither fixture had the field; nothing to assert


def test_numbers_are_decoded(fixture_images):
    _, images = fixture_images
    parsed = infotext.parse(png_io.read(images[0]).infotext)
    assert isinstance(parsed["fields"]["Steps"], int)
    assert isinstance(parsed["fields"]["CFG scale"], float)


def test_prompt_and_negative_prompt_are_separated(fixture_images):
    _, images = fixture_images
    parsed = next(
        infotext.parse(png_io.read(path).infotext)
        for path in images
        if "Negative prompt:" in (png_io.read(path).infotext or "")
    )
    assert parsed["prompt"]
    assert parsed["negative_prompt"]
    assert "Negative prompt:" not in parsed["prompt"]


def test_lora_tags_are_read_in_order(fixture_images):
    _, images = fixture_images
    parsed = infotext.parse(png_io.read(images[0]).infotext)
    tags = infotext.lora_tags(parsed["prompt"])
    assert tags
    assert all(isinstance(weight, float) for _, weight in tags)


def test_empty_input_does_not_raise():
    for value in (None, "", "   "):
        assert infotext.parse(value)["fields"] == {}


def test_detector_names_a1111_and_reports_foreign_metadata_as_unknown():
    assert png_io.detect_metadata_family({"parameters": "prompt"}, None) == "a1111"
    assert (
        png_io.detect_metadata_family(
            {"prompt": '{"6": {}}', "workflow": '{"nodes": []}'}, None
        )
        == "unknown"
    )


def test_foreign_metadata_is_listed_and_marked_unreadable(client, tmp_path):
    from PIL import Image, PngImagePlugin

    from backend import jobs
    from backend.scanner import service
    from backend.store import images as image_store

    folder = tmp_path / "foreign"
    folder.mkdir()
    path = folder / "comfy.png"
    chunks = PngImagePlugin.PngInfo()
    chunks.add_text("prompt", '{"6": {"class_type": "KSampler"}}')
    chunks.add_text("workflow", '{"nodes": []}')
    Image.new("RGB", (16, 16), "navy").save(path, pnginfo=chunks)
    client.post("/api/sources", json={"path": str(folder), "label": "Foreign"})

    totals = service.scan_roots(jobs.Job(id=0, kind="scan"))
    stored = image_store.get_by_path(str(path))

    assert totals["scanned"] == 1
    assert totals["failed"] == 0
    assert stored is not None
    assert stored["raw_infotext"] is None
    assert stored["parsed"]["prompt"] == ""
    assert stored["parsed"]["fields"] == {}
    assert stored["parsed"]["warnings"] == [png_io.UNKNOWN_METADATA_WARNING]


def test_an_a1111_reader_failure_does_not_remove_the_image(
    client, tmp_path, monkeypatch
):
    from PIL import Image, PngImagePlugin

    from backend import jobs
    from backend.scanner import service
    from backend.store import images as image_store

    folder = tmp_path / "unreadable"
    folder.mkdir()
    path = folder / "broken-metadata.png"
    chunks = PngImagePlugin.PngInfo()
    chunks.add_text("parameters", "prompt\nSteps: 8, Sampler: Euler, CFG scale: 1")
    Image.new("RGB", (16, 16), "navy").save(path, pnginfo=chunks)
    client.post("/api/sources", json={"path": str(folder), "label": "Unreadable"})

    def fail_to_read(*_args):
        raise ValueError("broken metadata")

    monkeypatch.setattr(infotext, "read", fail_to_read)
    totals = service.scan_roots(jobs.Job(id=0, kind="scan"))
    stored = image_store.get_by_path(str(path))

    assert totals["scanned"] == 1
    assert totals["failed"] == 0
    assert stored is not None
    assert stored["parsed"]["warnings"] == [png_io.UNREADABLE_METADATA_WARNING]


def test_a_scan_builds_the_field_inventory_without_inflating_counts(client, fixture_images):
    folder, images = fixture_images
    client.post("/api/sources", json={"path": str(folder), "label": "Test"})

    from backend import jobs
    from backend.scanner import service

    expected: Counter[str] = Counter()
    for path in images:
        expected.update(infotext.parse(png_io.read(path).infotext)["fields"].keys())

    service.scan_roots(jobs.Job(id=0, kind="scan"))
    first = client.get("/api/settings/metadata-fields")

    assert first.status_code == 200
    assert {
        item["field_name"]: item["image_count"] for item in first.json()["items"]
    } == expected
    assert first.json()["items"] == sorted(
        first.json()["items"],
        key=lambda item: (-item["image_count"], item["field_name"].lower(), item["field_name"]),
    )
    assert all(item["last_seen_at"].endswith("Z") for item in first.json()["items"])

    service.scan_roots(jobs.Job(id=1, kind="scan"))
    second = client.get("/api/settings/metadata-fields").json()["items"]

    assert {item["field_name"]: item["image_count"] for item in second} == expected


def test_recording_metadata_fields_replaces_one_images_membership(scanned, monkeypatch):
    from backend import db
    from backend.store import images as image_store

    monkeypatch.setattr(db, "now_iso", lambda: "2026-08-25T10:00:00Z")
    image_store.record_metadata_fields(scanned[0], ["Inventory alpha", "Inventory beta"])
    image_store.record_metadata_fields(scanned[0], ["Inventory alpha", "Inventory beta"])

    inventory = {item["field_name"]: item for item in image_store.metadata_field_inventory()}
    assert inventory["Inventory alpha"] == {
        "field_name": "Inventory alpha",
        "image_count": 1,
        "last_seen_at": "2026-08-25T10:00:00Z",
    }

    image_store.record_metadata_fields(scanned[0], ["Inventory beta"])
    inventory = {item["field_name"]: item for item in image_store.metadata_field_inventory()}
    assert inventory["Inventory alpha"]["image_count"] == 0
    assert inventory["Inventory beta"]["image_count"] == 1


def test_a_saved_inventory_toggle_changes_the_materialised_upload_copy(
    client, scanned, tmp_path
):
    from backend.posts import materialise
    from backend.store import images as image_store

    image = next(
        item for item in image_store.get_many(scanned) if "Model" in item["parsed"]["fields"]
    )
    image["source_path"] = image["absolute_path"]

    response = client.put("/api/settings", json={"metadata_excluded_fields": ["Model"]})
    upload_text = materialise.upload_infotext(image)
    target = materialise.path_for(image, tmp_path / "upload", upload_text)

    assert response.status_code == 200
    assert response.json()["metadata_excluded_fields"] == ["Model"]
    assert "Model" not in infotext.parse(png_io.read(target).infotext)["fields"]


def test_a_plain_prompt_is_not_mistaken_for_a_parameter_line():
    """Three "key: value" pairs is the threshold; a prompt with a colon in it
    must not be swallowed."""
    parsed = infotext.parse("a girl, looking at viewer: closeup")
    assert parsed["prompt"].startswith("a girl")
    assert parsed["fields"] == {}


def test_hash_maps_are_parsed():
    result = infotext.parse_hash_map("nameA: abc123, nameB: def456")
    assert result == {"nameA": "abc123", "nameB": "def456"}


def test_the_checkpoint_is_never_credited_twice(fixture_images):
    """Matching credits by name alone produced the model twice - once from the
    `Model` field and once from the leftover pool."""
    _, images = fixture_images
    for path in images:
        parsed = infotext.parse(png_io.read(path).infotext)
        found = resource_extract.extract(parsed)
        checkpoints = [r for r in found if r["resource_type"] == "checkpoint"]
        assert len(checkpoints) <= 1, f"{path.name}: {len(checkpoints)} Checkpoints"


def test_model_parameter_alone_is_checkpoint_evidence():
    parsed = infotext.parse(
        "plain prompt\nSteps: 8, Sampler: Euler, CFG scale: 1, "
        "Model: MoonMasterILL_V1.0Fafnir"
    )

    assert resource_extract.extract(parsed) == [
        {
            "resource_type": "checkpoint",
            "name_in_prompt": "MoonMasterILL_V1.0Fafnir",
            "hash": None,
            "hash_prefix": "",
            "weight": None,
            "model_id": None,
            "model_version_id": None,
            "model_name": None,
            "version_name": None,
            "resolved_from": "unresolved",
        }
    ]


def test_the_reforge_shape_makes_one_row_per_hash_entry():
    """Pins the real reForge file end to end: the hash maps decide which rows
    exist, and the prompt only supplies their weights.

    It does not by itself distinguish the evidence rule from the old
    prompt-driven path - for that shape both produce the same rows. The
    discriminating case is `test_a_prompt_tag_without_parameter_evidence_creates_no_row`.
    """
    found = resource_extract.extract(infotext.parse(REFORGE_INFOTEXT))
    by_name = {resource["name_in_prompt"]: resource for resource in found}

    fields = infotext.parse(REFORGE_INFOTEXT)["fields"]
    assert {r["name_in_prompt"] for r in found if r["resource_type"] == "lora"} == set(
        infotext.parse_hash_map(fields["Lora hashes"])
    ), "the rows are exactly the hash entries, no more and no fewer"

    assert by_name["zy_Inflatable_World_Morph _v1"]["weight"] == 1.0
    assert by_name["MoonTasticCuteCreatures"]["weight"] == 0.6
    assert by_name["T0m4t0M0rph"]["weight"] == 0.8
    assert by_name["text"]["weight"] == 0.2
    assert by_name["textxl"]["weight"] == 0.4
    assert by_name["lazypos"]["resource_type"] == "embedding"
    assert by_name["lazyneg"]["resource_type"] == "embedding"


def test_hash_and_credit_evidence_merge_into_one_row_per_lora():
    from backend.resources import cache

    identities = [
        ("99A78D2E68FD", 639347, "[Style/XL] Inflatable World Morph"),
        ("763802A3A5AA", 2173174, "MoonTastic – Cute Creatures"),
    ]
    for digest, version_id, model_name in identities:
        cache.put(
            digest,
            {
                "id": version_id,
                "modelId": version_id - 1,
                "name": "v1",
                "model": {"name": model_name, "type": "LoRA"},
            },
            source="manual",
            status=200,
        )
    parsed = infotext.parse(
        "<lora:zy_Inflatable_World_Morph _v1:1.0>, "
        "<lora:MoonTasticCuteCreatures:0.6>\n"
        "Steps: 8, Sampler: Euler, CFG scale: 1, "
        'Lora hashes: "zy_Inflatable_World_Morph _v1: 99A78D2E68FD, '
        'MoonTasticCuteCreatures: 763802A3A5AA", '
        'Civitai resources: [{"type":"lora","weight":1.0,'
        '"modelVersionId":639347,"modelName":"[Style/XL] Inflatable World Morph"},'
        '{"type":"lora","weight":0.6,"modelVersionId":2173174,'
        '"modelName":"MoonTastic – Cute Creatures"}]'
    )

    found = resource_extract.extract(parsed)

    assert len(found) == 2
    assert {
        row["name_in_prompt"]: (row["hash"], row["model_version_id"], row["weight"])
        for row in found
    } == {
        "zy_Inflatable_World_Morph _v1": ("99A78D2E68FD", 639347, 1.0),
        "MoonTasticCuteCreatures": ("763802A3A5AA", 2173174, 0.6),
    }


def test_consolidated_hash_name_links_after_normalisation():
    parsed = infotext.parse(
        "<lora:MyLoRA:0.7>\nSteps: 8, Sampler: Euler, CFG scale: 1, "
        'Hashes: {"LoRA:my lora": "ABCDEF123456"}'
    )

    found = resource_extract.extract(parsed)

    assert [(row["name_in_prompt"], row["hash"], row["weight"]) for row in found] == [
        ("MyLoRA", "ABCDEF123456", 0.7)
    ]


def test_each_lora_is_credited_with_its_own_model():
    found = resource_extract.extract(infotext.parse(CLOSE_PNG_INFOTEXT))
    loras = [resource for resource in found if resource["resource_type"] == "lora"]
    checkpoint = next(
        resource for resource in found if resource["resource_type"] == "checkpoint"
    )

    assert len(loras) == 2
    assert {resource["name_in_prompt"]: resource["model_version_id"] for resource in loras} == {
        "GrandlineLSIv1.0Krea": 3161188,
        "AfterveilWornMemoryStylev1.0Krea": 3197671,
    }
    assert all(resource["resolved_from"] != "unresolved" for resource in loras)
    assert checkpoint["name_in_prompt"] == "krea2_turbo_fp8_scaled"


def test_a_prompt_tag_without_parameter_evidence_creates_no_row():
    parsed = infotext.parse(
        "<lora:X:0.65>\nSteps: 8, Sampler: Euler, CFG scale: 1"
    )

    assert resource_extract.extract(parsed) == []


def test_weight_matches_only_one_unconsumed_credit():
    parsed = infotext.parse(
        '<lora:SandSwirlLoRa:0.65>\nSteps: 8, Sampler: Euler, CFG scale: 1, '
        'Civitai resources: [{"type":"lora","weight":0.65000001,'
        '"modelVersionId":123,"modelName":"Made of Sand and Shells"}]'
    )

    found = resource_extract.extract(parsed)

    assert len(found) == 1
    assert found[0]["name_in_prompt"] == "SandSwirlLoRa"
    assert found[0]["model_version_id"] == 123


def test_ambiguous_weight_does_not_match():
    parsed = infotext.parse(
        '<lora:SandSwirlLoRa:0.65>\nSteps: 8, Sampler: Euler, CFG scale: 1, '
        'Civitai resources: [{"type":"lora","weight":0.65,'
        '"modelVersionId":123,"modelName":"Made of Sand and Shells"},'
        '{"type":"lora","weight":0.65,"modelVersionId":456,'
        '"modelName":"Unrelated credit"}]'
    )

    found = resource_extract.extract(parsed)
    assert len(found) == 2
    assert {resource["name_in_prompt"] for resource in found} == {
        "Made of Sand and Shells",
        "Unrelated credit",
    }


def test_credit_hash_on_a_later_row_beats_an_earlier_name_match():
    parsed = infotext.parse(
        "prompt\nSteps: 8, Sampler: Euler, CFG scale: 1, "
        'Lora hashes: "Alpha: AAAAAAAAAA, Beta: BBBBBBBBBB", '
        'Civitai resources: [{"type":"lora","hash":"BBBBBBBBBB",'
        '"modelVersionId":999,"modelName":"Alpha"}]'
    )

    found = resource_extract.extract(parsed)
    by_name = {row["name_in_prompt"]: row for row in found}

    assert by_name["Alpha"]["model_version_id"] is None
    assert by_name["Beta"]["model_version_id"] == 999


def test_schema_nine_reextracts_stored_resources(scanned):
    from backend import db
    from backend.store import images as image_store

    image_id = scanned[0]
    conn = db.get_connection()
    conn.execute(
        "UPDATE images SET raw_infotext=? WHERE id=?",
        (CLOSE_PNG_INFOTEXT, image_id),
    )
    conn.commit()
    image_store.replace_resources(
        image_id,
        [
            {
                "resource_type": "lora",
                "name_in_prompt": "GrandlineLSIv1.0Krea",
                "model_version_id": 3197671,
                "resolved_from": "embedded",
            },
            {
                "resource_type": "lora",
                "name_in_prompt": "AfterveilWornMemoryStylev1.0Krea",
                "resolved_from": "unresolved",
            },
            {
                "resource_type": "lora",
                "name_in_prompt": "Grandline - Legendary Stylized Illustration",
                "model_version_id": 3161188,
                "resolved_from": "embedded",
            },
        ],
    )
    conn.execute("PRAGMA user_version=8")
    conn.commit()

    db.init_db()

    loras = [
        resource
        for resource in image_store.resources_for(image_id)
        if resource["resource_type"] == "lora"
    ]
    assert len(loras) == 2
    assert {resource["name_in_prompt"]: resource["model_version_id"] for resource in loras} == {
        "GrandlineLSIv1.0Krea": 3161188,
        "AfterveilWornMemoryStylev1.0Krea": 3197671,
    }


def test_hash_prefix_normalises_the_three_lengths():
    """AutoV2 is 10 chars, `Lora hashes` often 12, `Hashes` sometimes all 64 -
    the same file, three spellings."""
    assert (
        resource_extract.hash_prefix("d716ef6a78")
        == resource_extract.hash_prefix("D716EF6A78FD")
        == resource_extract.hash_prefix("d716ef6a78fd0011223344556677")
        == "D716EF6A78"
    )
    assert resource_extract.hash_prefix(None) == ""


def test_the_display_view_groups_the_important_fields(fixture_images):
    _, images = fixture_images
    parsed = infotext.parse(png_io.read(images[0]).infotext)
    view = display.render(parsed, resource_extract.extract(parsed))

    assert view["has_metadata"]
    assert [item["label"] for item in view["primary"]][:2] == ["Model", "Sampler"]
    # raw representations are shown as cards, never as plain rows
    keys = {item["key"] for item in view["other"]}
    assert "Civitai resources" not in keys
    assert "Lora hashes" not in keys


def test_every_resolved_resource_is_linkable(fixture_images):
    """The embedded credits carry modelVersionId but usually no modelId, so a
    link built only from the model id would leave most of them dead."""
    _, images = fixture_images
    parsed = infotext.parse(png_io.read(images[0]).infotext)
    view = display.render(parsed, resource_extract.extract(parsed))

    from backend import config

    resolved = [card for card in view["resources"] if card["resolved"]]
    assert resolved, "the fixture has resolved resources"
    for card in resolved:
        assert card["url"], f"{card['name']} is resolved but not linked"
        assert card["url"].startswith(config.site_base())


def test_links_follow_the_configured_domain(fixture_images):
    """The default is .red, because .com filters to SFW in some regions."""
    from backend import config, db

    parsed = infotext.parse(png_io.read(fixture_images[1][0]).infotext)
    resources = resource_extract.extract(parsed)

    assert config.site_base() == "https://civitai.red", "Vorgabe"

    db.set_setting("site_base", "https://civitai.com")
    view = display.render(parsed, resources)
    linked = [card for card in view["resources"] if card["url"]]
    assert linked and all(card["url"].startswith("https://civitai.com/") for card in linked)
