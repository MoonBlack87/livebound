"""The structured document sent with every CivitAI image."""

from __future__ import annotations

import json

import pytest
from PIL import Image, PngImagePlugin

# Not `from test.backend...`: Python's own standard library ships a `test`
# package, and a regular package beats this directory's namespace portion
# wherever it is installed. pytest puts this directory on the path, so the
# bare module name is both shorter and the one that always resolves.
from test_model_hashing import REAL_UPSCALER_INFOTEXT

from backend import db
from backend.metadata import (
    compose,
    families,
    infotext,
    png_io,
    resource_edits,
    resources,
    upload_document,
)
from backend.posts import materialise
from backend.resources import cache
from backend.store import images as image_store
from backend.store import model_roots

_REAL_PARSED = infotext.parse(REAL_UPSCALER_INFOTEXT)

# Captured from post.getEdit image 140647535. The baseline predates RES-13, so
# its generic `resources` array is deliberately absent from the new rendering.
CAPTURED_BASELINE_META = {
    "Denoising strength": "0.45",
    "ENSD": "31337",
    "Eta": "0.67",
    "Hires CFG Scale": "5.5",
    "Hires prompt": _REAL_PARSED["fields"]["Hires prompt"],
    "Hires steps": "10",
    "Hires upscale": "1.25",
    "Hires upscaler": "4x_foolhardy_Remacri",
    "Model": "MoonArtCauldronMix_XL_V1.0.0",
    "Model hash": "d716ef6a78",
    "Prompt Enhancer": "placeholder",
    "Schedule type": "Karras",
    "TI hashes": {"lazyneg": "ba21023c7054", "lazypos": "30866692653c"},
    "cfgScale": 5.5,
    "clipSkip": 2,
    "hashes": {
        "lora:Bones_World_Morph": "f1abb61d3082",
        "lora:FloatingAndRiding-SDXL_epoch_9": "acf36adf9a6f",
        "lora:SkullsfortheSkullThrone": "743348a3277a",
        "model": "d716ef6a78",
    },
    "height": 1216,
    "negativePrompt": _REAL_PARSED["negative_prompt"],
    "prompt": _REAL_PARSED["prompt"],
    "resources": [
        {
            "hash": "acf36adf9a6f",
            "name": "FloatingAndRiding-SDXL_epoch_9",
            "type": "lora",
            "unmatched": True,
            "weight": 0.75,
        },
        {
            "hash": "743348a3277a",
            "name": "SkullsfortheSkullThrone",
            "type": "lora",
            "weight": 0.7,
        },
        {
            "hash": "f1abb61d3082",
            "name": "Bones_World_Morph",
            "type": "lora",
            "weight": 0.45,
        },
        {
            "hash": "d716ef6a78",
            "name": "MoonArtCauldronMix_XL_V1.0.0",
            "type": "model",
        },
    ],
    "sampler": "Euler a",
    "seed": 1801438905,
    "steps": 20,
    "width": 896,
}


def _install_upscaler(tmp_path) -> None:
    digest = "E1A73BD89C2DA1AE494774746398689048B5A892BD9653E146713F9DF8BCA86A"
    folder = tmp_path / "models"
    folder.mkdir()
    root = model_roots.add_root(str(folder))
    model_roots.upsert_file(
        root["id"],
        folder / "4x_foolhardy_Remacri.pth",
        "4x_foolhardy_Remacri.pth",
        mtime=1,
        size=100,
        sha256=digest,
        file_stem="4x_foolhardy_Remacri",
    )
    cache.assign_manual(digest, 147759, 164821)


def _project_real_document(image_id: int, tmp_path) -> tuple[str, dict]:
    _install_upscaler(tmp_path)
    found = resources.extract(_REAL_PARSED)
    cache.enrich(found)
    image_store.replace_resources(image_id, found)
    upload = resource_edits.build_document(
        REAL_UPSCALER_INFOTEXT, REAL_UPSCALER_INFOTEXT, image_id
    )
    projected = upload_document.render_infotext(upload)
    document = upload_document.render_meta(upload)
    assert document is not None
    return projected, document


def test_real_infotext_matches_the_captured_civitai_document(scanned, tmp_path):
    projected, document = _project_real_document(scanned[0], tmp_path)

    assert set(document) == (
        set(CAPTURED_BASELINE_META) - {"hashes", "resources", "Model hash", "TI hashes"}
    ) | {
        "civitaiResources"
    }
    for key, value in CAPTURED_BASELINE_META.items():
        if key in ("hashes", "resources", "Model hash", "TI hashes"):
            continue
        assert document[key] == value, key

    assert "hashes" not in document
    assert "resources" not in document
    projected_hires_prompt = infotext.parse(projected)["fields"]["Hires prompt"]
    assert isinstance(document["Hires prompt"], str)
    assert document["Hires prompt"] == projected_hires_prompt
    assert {"type": "upscaler", "modelVersionId": 164821} in document[
        "civitaiResources"
    ]
    assert isinstance(document["Model"], str) and not _is_image_meta_on_site(document)


def test_exclusions_hide_fields_and_drop_their_derived_attribution(scanned, tmp_path):
    projected, _document = _project_real_document(scanned[0], tmp_path)
    image = {
        "image_id": scanned[0],
        "raw_infotext": REAL_UPSCALER_INFOTEXT,
        "sha256": "A" * 64,
    }
    db.set_json_setting(materialise.EXCLUDED_FIELDS_SETTING, ["Sampler", "Hires *"])

    text = materialise.upload_infotext(image)
    document = materialise.upload_document(image)
    assert document is not None
    assert text != projected
    fields = infotext.parse(text)["fields"]
    assert "Sampler" not in fields
    assert not any(key.startswith("Hires ") for key in fields)
    assert "sampler" not in document
    assert not any(key.startswith("Hires ") for key in document)
    assert not any(
        entry.get("type") == resources.UPSCALER
        for entry in document.get("civitaiResources", [])
    )


def test_empty_infotext_has_no_upload_document():
    assert upload_document.render_meta(upload_document.build("", [])) is None


def test_credits_without_model_cannot_claim_the_image_was_generated_on_site():
    upload = upload_document.build(
        "a prompt\nSteps: 20, Sampler: Euler a, CFG scale: 5.5, Seed: 1, "
        'Size: 896x1216, Civitai resources: [{"type":"checkpoint",'
        '"modelVersionId":1970825}]',
        [
            {
                "resource_type": resources.CHECKPOINT,
                "name_in_prompt": "External Checkpoint",
                "model_version_id": 1970825,
            }
        ],
    )
    document = upload_document.render_meta(upload)

    assert document is not None
    assert document["Model"] == "External Checkpoint"
    assert document["civitaiResources"] == [
        {"type": "checkpoint", "modelVersionId": 1970825}
    ]
    assert not _is_image_meta_on_site(document)


def test_excluding_model_drops_its_checkpoint_credit(scanned, tmp_path):
    _project_real_document(scanned[0], tmp_path)
    image = {
        "image_id": scanned[0],
        "raw_infotext": REAL_UPSCALER_INFOTEXT,
        "sha256": "A" * 64,
    }
    db.set_json_setting(materialise.EXCLUDED_FIELDS_SETTING, ["Model"])

    text = materialise.upload_infotext(image)
    document = materialise.upload_document(image)

    assert "Model" not in infotext.parse(text)["fields"]
    assert document is not None
    assert "Model" not in document
    assert not any(
        entry.get("type") == resources.CHECKPOINT
        for entry in document.get("civitaiResources", [])
    )
    # And the classification that follows is the user's own choice, stated
    # before they make it: the metadata settings warn that without `Model` or
    # `Version` CivitAI reads the upload as generated on its site (`MET-11`),
    # and the editor shows the state per image. The remaining credits still go
    # out - `MET-16` has the CivitAI block carry everything the application
    # derived, and withholding it to dodge a true classification would state
    # less than is known. Changed 2026-09-06 with the maintainer, for the
    # images CivitAI itself generated: those have no checkpoint name at all.
    assert _is_image_meta_on_site(document)


def test_resource_rows_cannot_land_in_both_document_arrays():
    full_hash = "BA21023C7054D4488F77" + "A" * 44
    cache.put(
        full_hash,
        {
            "id": 2121199,
            "modelId": 1,
            "name": "lazyneg",
            "model": {"name": "lazyneg", "type": "TextualInversion"},
        },
        source="rest",
        status=200,
    )
    assert cache.get("BA21023C7054")["model_version_id"] == 2121199
    text = (
        "prompt\nSteps: 8, Sampler: Euler, CFG scale: 1, Model: External, "
        'TI hashes: "lazyneg: ba21023c7054", '
        'Civitai resources: [{"type":"embedding","modelVersionId":1860747}]'
    )
    upload = upload_document.build(
        text,
        [
            {
                "resource_type": resources.EMBEDDING,
                "name_in_prompt": "lazyneg",
                "hash": "BA21023C7054",
                "model_version_id": 1860747,
            }
        ],
    )
    document = upload_document.render_meta(upload)

    assert document is not None
    assert document["civitaiResources"] == [
        {"type": "embedding", "modelVersionId": 1860747}
    ]
    assert "resources" not in document


def test_unresolvable_resource_stays_in_a1111_hashes_but_not_meta():
    text = (
        "a scene\nSteps: 8, Sampler: Euler, CFG scale: 1, Model: External, "
        'Lora hashes: "UnknownStyle: abcdef1234"'
    )
    upload = upload_document.build(
        text,
        [
            {
                "resource_type": resources.LORA,
                "name_in_prompt": "UnknownStyle",
                "hash": "ABCDEF1234",
                "weight": 0.7,
                "model_version_id": None,
            }
        ],
    )

    projected = infotext.parse(upload_document.render_infotext(upload))
    document = upload_document.render_meta(upload)

    assert infotext.parse_hash_map(projected["fields"]["Lora hashes"]) == {
        "UnknownStyle": "abcdef1234"
    }
    assert document is not None
    assert "resources" not in document
    assert "civitaiResources" not in document


@pytest.fixture
def hash_spelling_document():
    text = (
        "a scene\nSteps: 8, Sampler: Euler, CFG scale: 1, Model: External, "
        "Model hash: d716ef6a78, "
        'Hashes: {"upscaler:Existing": "C0FFEE123456"}, '
        'Lora hashes: "Generator alias: aBcDeF1234ab", '
        'TI hashes: "Embedding alias: 0123aBcDeF45"'
    )
    return (
        text,
        [
            {
                "resource_type": resources.CHECKPOINT,
                "name_in_prompt": "External",
                "hash": "D716EF6A78" + "0" * 54,
            },
            {
                "resource_type": resources.LORA,
                "name_in_prompt": "Canonical lora name",
                "hash": "ABCDEF1234AB" + "1" * 52,
            },
            {
                "resource_type": resources.EMBEDDING,
                "name_in_prompt": "Canonical embedding name",
                "hash": "0123ABCDEF45" + "2" * 52,
            },
        ],
    )


def test_repeated_hashes_use_one_existing_spelling_throughout_the_document(
    hash_spelling_document,
):
    text, resource_rows = hash_spelling_document
    upload = upload_document.build(text, resource_rows)

    rendered = upload_document.render_infotext(upload)
    spellings = _resource_hash_spellings(rendered)
    repeated = {prefix: values for prefix, values in spellings.items() if len(values) > 1}

    assert repeated
    assert all(len(set(values)) == 1 for values in repeated.values()), repeated
    fields = infotext.parse(rendered)["fields"]
    assert fields["Model hash"] == "d716ef6a78"
    assert infotext.parse_hash_map(fields["Lora hashes"])["Generator alias"] == (
        "aBcDeF1234ab"
    )
    assert infotext.parse_hash_map(fields["TI hashes"])["Embedding alias"] == (
        "0123aBcDeF45"
    )


def _resource_hash_spellings(text: str) -> dict[str, list[str]]:
    fields = infotext.parse(text)["fields"]
    found: dict[str, list[str]] = {}
    for key in ("Model hash", "Hashes", "Lora hashes", "TI hashes"):
        value = fields.get(key)
        if value is None:
            continue
        if key == "Model hash":
            candidates = [str(value)]
        elif isinstance(value, dict):
            candidates = [str(digest) for digest in value.values()]
        else:
            candidates = [
                digest.strip()
                for item in str(value).split(",")
                if (digest := item.partition(":")[2].strip())
            ]
        for digest in candidates:
            found.setdefault(resources.hash_prefix(digest), []).append(digest)
    return found


def test_fixture_renderings_preserve_parameters_and_share_resource_values(scanned):
    """The fixture-wide property owed by MET-15, MET-19, and MET-20."""

    def excluded(key: str, patterns: list[str]) -> bool:
        return any(
            key.startswith(pattern[:-1]) if pattern.endswith("*") else key == pattern
            for pattern in patterns
        )

    for patterns in ([], ["Eta", "Hires *"]):
        db.set_json_setting(materialise.EXCLUDED_FIELDS_SETTING, patterns)
        for image_id in scanned:
            image = image_store.get(image_id)
            assert image is not None
            upload = materialise._upload_projection(image)
            rebuilt = upload_document.render_infotext(upload)
            meta = upload_document.render_meta(upload) or {}
            source = infotext.parse(image["raw_infotext"])
            projected = infotext.parse(rebuilt)

            _, source_line = compose._split(image["raw_infotext"])
            source_entries, _ = infotext.parse_parameter_line(source_line)
            _, rebuilt_line = compose._split(rebuilt)
            rebuilt_entries, _ = infotext.parse_parameter_line(rebuilt_line)

            expected_plain = [
                (entry.key, entry.source)
                    for entry in source_entries
                    if entry.key not in upload_document.RESOURCE_FIELDS
                    and entry.key != upload_document.LIVEBOUND_KEY
                    and not excluded(entry.key, patterns)
            ]
            actual_plain = [
                (entry.key, entry.source)
                    for entry in rebuilt_entries
                    if entry.key not in upload_document.RESOURCE_FIELDS
                    and entry.key != upload_document.LIVEBOUND_KEY
            ]
            assert actual_plain == expected_plain, image["relative_path"]

            expected_keys = {
                entry.key for entry in source_entries if not excluded(entry.key, patterns)
            }
            assert expected_keys <= set(projected["fields"]), image["relative_path"]

            source_fields = source["fields"]
            projected_fields = projected["fields"]
            for key in ("Hashes", "Lora hashes", "TI hashes"):
                if key in source_fields and not excluded(key, patterns):
                    assert infotext.parse_hash_map(projected_fields[key]) == (
                        infotext.parse_hash_map(source_fields[key])
                    ), (image["relative_path"], key)
            if "Model hash" in source_fields and not excluded("Model hash", patterns):
                assert projected_fields["Model hash"] == source_fields["Model hash"]

            assert projected_fields.get("Civitai resources", []) == meta.get(
                "civitaiResources", []
            )
            assert "resources" not in meta



def _stated(value):
    """A field value as the block states it, independent of how it round-trips.

    A parameter line is text, so a value's Python type does not survive unless
    the field is one this application coerces everywhere - `Sharpness: 2` is the
    same fact whether it comes back as `2` or `"2"`. A structured value such as
    `Civitai resources` comes back with its keys in the order JSON wrote them,
    which is likewise not part of what it states.
    """
    import json

    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return compose.render(value)


def _container_metadata_values(path):
    """Every text chunk and EXIF value carried by a materialised file."""
    facts = png_io.read(path)
    assert facts is not None
    values = list(facts.chunks.values())
    if facts.user_comment is not None:
        values.append(facts.user_comment)
    with Image.open(path) as produced:
        exif = produced.getexif()
        values.extend(str(value) for value in exif.values())
        for ifd in (0x8769,):
            values.extend(str(value) for value in exif.get_ifd(ifd).values())
    return values


@pytest.mark.parametrize("family", ("a1111", "swarmui", "ruinedfooocus", "comfyui", "tensorart"))
def test_every_family_materialises_a_replaced_prompt_without_the_old_value(family, tmp_path):
    from test_metadata_families import A1111, COMFY_GRAPH, RUINED_FOOOCUS, SWARMUI, TENSORART

    from backend.metadata import comfyui, ruinedfooocus, swarmui, tensorart

    comfy_raw = COMFY_GRAPH.replace(
        '"text_g":"a real corpus prompt","text_l":"more detail"',
        '"text_g":"old-comfy","text_l":"old-comfy"',
    )
    raw, parsed, keyword = {
        "a1111": (A1111, infotext.parse(A1111), "parameters"),
        "swarmui": (SWARMUI, swarmui.parse(SWARMUI), "parameters"),
        "ruinedfooocus": (RUINED_FOOOCUS, ruinedfooocus.parse(RUINED_FOOOCUS), "parameters"),
        "comfyui": (comfy_raw, comfyui.parse(comfy_raw), "prompt"),
        "tensorart": (
            TENSORART,
            tensorart.read({"generation_data": TENSORART}, None),
            "parameters",
        ),
    }[family]
    source = tmp_path / f"{family}.png"
    info = PngImagePlugin.PngInfo()
    info.add_text(keyword, raw)
    Image.new("RGB", (64, 64)).save(source, "PNG", pnginfo=info)
    image = {"id": None, "source_path": str(source), "raw_infotext": raw, "parsed": parsed}
    text = materialise.upload_infotext(
        image,
        edit_document={
            "draft": {"prompt": f"replacement-{family}", "fields": {}},
            "touched": ["prompt"],
            "deleted": [],
        },
    )
    target = materialise.path_for(image, tmp_path, text)
    payload = "\n".join(_container_metadata_values(target))

    assert parsed["prompt"] not in payload
    assert f"replacement-{family}" in payload


def test_every_family_uses_the_structure_civitai_can_read(monkeypatch):
    """The opaque families get A1111; the native readers keep their grammar."""
    from test_metadata_families import (
        A1111,
        CIVITAI_GRAPH,
        COMFY_GRAPH,
        FOOOCUS,
        FOOOCUS_PLUS,
        HF_SPACE,
        INVOKEAI,
        NOVELAI_CHUNKS,
        NOVELAI_COMMENT,
        RUINED_FOOOCUS,
        SWARMUI,
        TENSORART,
    )

    from backend.metadata import (
        civitai_generated,
        comfyui,
        fooocus,
        fooocusplus,
        hfspace,
        invokeai,
        novelai,
        ruinedfooocus,
        swarmui,
        tensorart,
    )

    a1111_readings = [
        ("a1111", A1111, infotext.parse(A1111)),
        ("ruinedfooocus", RUINED_FOOOCUS, ruinedfooocus.parse(RUINED_FOOOCUS)),
        ("fooocus", FOOOCUS, fooocus.parse(FOOOCUS)),
        ("fooocusplus", FOOOCUS_PLUS, fooocusplus.parse(FOOOCUS_PLUS)),
        ("novelai", NOVELAI_COMMENT, novelai.read(NOVELAI_CHUNKS, None)),
        ("invokeai", INVOKEAI, invokeai.read({"invokeai_metadata": INVOKEAI}, None)),
        ("hf-space", HF_SPACE, hfspace.read({"parameters": HF_SPACE}, None)),
        ("tensorart", TENSORART, tensorart.read({"generation_data": TENSORART}, None)),
    ]

    for family, raw, parsed in a1111_readings:
        rows = resources.extract(parsed)
        monkeypatch.setattr(
            image_store, "resources_for", lambda _image_id, rows=rows: rows
        )
        projected = infotext.parse(
            materialise.upload_infotext(
                {"id": 1, "raw_infotext": raw, "parsed": parsed}
            )
        )
        assert projected["prompt"] == parsed["prompt"], family
        assert projected["negative_prompt"] == parsed["negative_prompt"], family
        # Compared as the block states them. A parameter line is text, so a
        # value's Python type does not survive the round trip unless the field
        # is one of the few this application coerces everywhere - and the fact
        # the upload carries is `Sharpness: 2` either way. Listing every
        # generator's own numeric field to keep the types matching would be a
        # treadmill that buys nothing the block does not already say.
        assert {key: _stated(value) for key, value in projected["fields"].items()} == {
            key: _stated(value)
            for key, value in {
                **parsed["fields"],
                **(
                    {"livebound": {"generator": families.origin_label(family)}}
                    if families.origin_label(family)
                    else {}
                ),
            }.items()
        }, family

    native_readings = [
        ("swarmui", SWARMUI, swarmui.parse(SWARMUI), {"parameters": SWARMUI}, None),
        ("comfyui", COMFY_GRAPH, comfyui.parse(COMFY_GRAPH), {"prompt": COMFY_GRAPH}, None),
        ("civitai", CIVITAI_GRAPH, civitai_generated.parse(CIVITAI_GRAPH), {}, CIVITAI_GRAPH),
    ]
    for family, raw, parsed, chunks, comment in native_readings:
        monkeypatch.setattr(image_store, "resources_for", lambda _image_id: [])
        text = materialise.upload_infotext({"id": 1, "raw_infotext": raw, "parsed": parsed})
        assert png_io.detect_metadata_family(
            dict.fromkeys(chunks, text), text if comment is not None else None
        ) == family


def test_a1111_fixtures_keep_their_source_and_uploaded_text_as_before(scanned):
    for image_id in scanned:
        image = image_store.get(image_id)
        assert image is not None
        before = upload_document.render_infotext(
                resource_edits.build_document(
                    image["raw_infotext"],
                    image["raw_infotext"],
                    image_id,
                    source_generator=families.origin_label(
                        materialise._source_family(image["raw_infotext"])
                    ),
                )
        )
        assert materialise._source_infotext(image, image["raw_infotext"]) == image[
            "raw_infotext"
        ]
        assert materialise.upload_infotext(image) == before


def test_a_non_a1111_edit_and_exclusion_apply_to_the_rendered_block(monkeypatch):
    from test_metadata_families import SWARMUI

    from backend.metadata import swarmui

    parsed = swarmui.parse(SWARMUI)
    monkeypatch.setattr(image_store, "resources_for", lambda _image_id: [])
    db.set_json_setting(materialise.EXCLUDED_FIELDS_SETTING, ["Sampler"])

    projected = swarmui.parse(
        materialise.upload_infotext(
            {"id": 1, "raw_infotext": SWARMUI, "parsed": parsed},
            edit_document={
                "draft": {"fields": {"Steps": 31}},
                "touched": ["Steps"],
                "deleted": [],
            },
        )
    )

    assert projected["fields"]["Steps"] == 31
    assert "Sampler" not in projected["fields"]


def test_swarmui_resource_projection_updates_every_native_location(monkeypatch):
    from test_metadata_families import SWARMUI

    from backend.metadata import swarmui

    source = json.loads(SWARMUI)
    source["sui_image_params"]["loras"] = ["detail.safetensors"]
    source["sui_image_params"]["loraweights"] = [0.65]
    source["sui_models"][0]["weight"] = 0.65
    raw = json.dumps(source)
    parsed = swarmui.parse(raw)
    row = {
        "resource_type": "lora",
        "name_in_prompt": "detail",
        "hash": "abc",
        "weight": 0.9,
    }
    monkeypatch.setattr(image_store, "resources_for", lambda _image_id: [row])

    updated = json.loads(
        materialise.upload_infotext({"id": 1, "raw_infotext": raw, "parsed": parsed})
    )
    assert updated["sui_models"][0]["weight"] == 0.9
    assert updated["sui_image_params"]["loraweights"] == [0.9]

    monkeypatch.setattr(
        image_store, "resources_for", lambda _image_id: [{**row, "deleted_by_user": 1}]
    )
    removed = materialise.upload_infotext({"id": 1, "raw_infotext": raw, "parsed": parsed})
    assert "detail" not in removed
    assert "abc" not in removed
    assert "0.65" not in removed


def test_comfyui_resolved_addition_materialises_an_air_loader(monkeypatch, tmp_path):
    from backend.metadata import comfyui

    raw = json.dumps(
        {
            "Latent": {"class_type": "EmptyLatentImage", "inputs": {"width": 64, "height": 64}},
            "Lora": {
                "class_type": "LoraLoader",
                "inputs": {
                    "lora_name": "detail.safetensors",
                    "strength_model": 0.65,
                    "strength_clip": 0.65,
                },
            },
            "Sampler": {
                "class_type": "KSampler",
                "inputs": {"latent_image": ["Latent", 0], "seed": 7, "steps": 20, "cfg": 5},
            },
        }
    )
    rows = [
        {"resource_type": "lora", "name_in_prompt": "detail", "weight": 0.65},
        {
            "resource_type": "lora",
            "name_in_prompt": "added",
            "weight": 0.8,
            "model_version_id": 22222,
            "added_by_user": 1,
        },
    ]
    monkeypatch.setattr(image_store, "resources_for", lambda _image_id: rows)
    source = tmp_path / "addition.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", raw)
    Image.new("RGB", (64, 64)).save(source, "PNG", pnginfo=info)
    image = {"id": 1, "source_path": str(source), "raw_infotext": raw, "parsed": comfyui.parse(raw)}

    target = materialise.path_for(image, tmp_path, materialise.upload_infotext(image))
    output = png_io.read(target)

    assert output is not None
    assert "prompt" not in output.chunks and "workflow" not in output.chunks
    assert '"modelVersionId": 22222' in "\n".join(_container_metadata_values(target))


def test_comfyui_metadata_edit_uses_the_standard_block(tmp_path):
    from backend.metadata import comfyui

    raw = json.dumps(
        {
            "Latent": {"class_type": "EmptyLatentImage", "inputs": {"width": 64, "height": 64}},
            "Sampler": {
                "class_type": "KSampler",
                "inputs": {"latent_image": ["Latent", 0], "seed": 42424242, "steps": 1, "cfg": 1},
            },
        }
    )
    workflow = '{"nodes":[{"id":"Sampler","widgets_values":[42424242]}]}'
    source = tmp_path / "source.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", raw)
    info.add_text("workflow", workflow)
    Image.new("RGB", (64, 64)).save(source, "PNG", pnginfo=info)
    image = {
        "id": None,
        "source_path": str(source),
        "raw_infotext": raw,
        "parsed": comfyui.parse(raw),
    }
    text = materialise.upload_infotext(
        image,
        edit_document={"draft": {"fields": {"Seed": 51515151}}, "touched": ["Seed"], "deleted": []},
    )
    target = materialise.path_for(image, tmp_path, text)
    payload = "\n".join(_container_metadata_values(target))

    assert "42424242" not in payload
    assert "51515151" in payload


def test_comfyui_resource_edit_uses_the_standard_block(monkeypatch, tmp_path):
    from backend.metadata import comfyui

    raw = json.dumps(
        {
            "Positive": {"class_type": "CLIPTextEncode", "inputs": {"text": "scene"}},
            "Lora": {
                "class_type": "LoraLoader",
                "inputs": {
                    "lora_name": "review-lora.safetensors",
                    "strength_model": 0.654321,
                    "strength_clip": 0.654321,
                },
            },
            "Sampler": {
                "class_type": "KSampler",
                "inputs": {"positive": ["Positive", 0], "seed": 7, "steps": 20, "cfg": 5},
            },
        }
    )
    source = tmp_path / "resource.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", raw)
    info.add_text(
        "workflow",
        '{"nodes":[{"id":"Lora","widgets_values":["review-lora.safetensors",0.654321,0.654321]}]}',
    )
    Image.new("RGB", (64, 64)).save(source, "PNG", pnginfo=info)
    image = {
        "id": 1,
        "source_path": str(source),
        "raw_infotext": raw,
        "parsed": comfyui.parse(raw),
    }
    monkeypatch.setattr(
        image_store,
        "resources_for",
        lambda _image_id: [
            {
                "resource_type": "lora",
                "name_in_prompt": "review-lora",
                "weight": 0.912345,
                "locked_by_user": 1,
            }
        ],
    )

    target = materialise.path_for(image, tmp_path, materialise.upload_infotext(image))
    output = png_io.read(target)

    assert materialise.comfyui_workflow_replaced(image)
    assert output is not None
    assert "prompt" not in output.chunks and "workflow" not in output.chunks
    assert "0.654321" not in "\n".join(_container_metadata_values(target))
    assert "0.912345" in "\n".join(_container_metadata_values(target))


def test_comfyui_added_metadata_uses_the_standard_block(tmp_path):
    from backend.metadata import comfyui

    raw = json.dumps(
        {
            "Latent": {"class_type": "EmptyLatentImage", "inputs": {"width": 64, "height": 64}},
            "Sampler": {
                "class_type": "KSampler",
                "inputs": {"latent_image": ["Latent", 0], "seed": 7, "steps": 20, "cfg": 5},
            },
        }
    )
    source = tmp_path / "added-field.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", raw)
    Image.new("RGB", (64, 64)).save(source, "PNG", pnginfo=info)
    image = {
        "id": None,
        "source_path": str(source),
        "raw_infotext": raw,
        "parsed": comfyui.parse(raw),
    }
    edit = {
        "draft": {"fields": {"Review field": "new-value"}},
        "touched": ["Review field"],
        "deleted": [],
    }

    target = materialise.path_for(
        image, tmp_path, materialise.upload_infotext(image, edit_document=edit)
    )
    output = png_io.read(target)

    assert materialise.comfyui_workflow_replaced(image, edit_document=edit)
    assert output is not None
    assert "prompt" not in output.chunks
    assert infotext.parse(output.chunks["parameters"])["fields"]["Review field"] == "new-value"


def test_comfyui_non_png_block_carries_its_origin(tmp_path):
    from backend.metadata import comfyui

    raw = json.dumps(
        {
            "Latent": {"class_type": "EmptyLatentImage", "inputs": {"width": 64, "height": 64}},
            "Sampler": {
                "class_type": "KSampler",
                "inputs": {"latent_image": ["Latent", 0], "seed": 7, "steps": 20, "cfg": 5},
            },
        }
    )
    source = tmp_path / "fallback.webp"
    Image.new("RGB", (64, 64)).save(source, "WEBP")
    image = {
        "id": None,
        "source_path": str(source),
        "raw_infotext": raw,
        "parsed": comfyui.parse(raw),
    }

    target = materialise.path_for(image, tmp_path, materialise.upload_infotext(image))
    payload = "\n".join(_container_metadata_values(target))

    assert 'livebound: {"generator": "ComfyUI"}' in payload


def test_comfyui_edit_uses_the_standard_block_and_undo_restores_the_native_graph(tmp_path):
    from backend.metadata import comfyui

    raw = json.dumps(
        {
            "Latent": {"class_type": "EmptyLatentImage", "inputs": {"width": 64, "height": 64}},
            "Sampler": {
                "class_type": "KSampler",
                "inputs": {"latent_image": ["Latent", 0], "seed": 7, "steps": 20, "cfg": 5},
            },
        }
    )
    workflow = '{"nodes":[{"id":"Sampler","widgets_values":[7,20,5]}]}'
    source = tmp_path / "undo.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", raw)
    info.add_text("workflow", workflow)
    Image.new("RGB", (64, 64)).save(source, "PNG", pnginfo=info)
    image = {
        "id": None,
        "source_path": str(source),
        "raw_infotext": raw,
        "parsed": comfyui.parse(raw),
    }
    edit = {"draft": {"fields": {"Seed": 8}}, "touched": ["Seed"], "deleted": []}

    changed = materialise.path_for(
        image, tmp_path, materialise.upload_infotext(image, edit_document=edit)
    )
    changed_facts = png_io.read(changed)
    restored = materialise.path_for(
        image, tmp_path, materialise.upload_infotext(image, edit_document=None)
    )
    restored_facts = png_io.read(restored)

    assert materialise.comfyui_workflow_replaced(image, edit_document=edit)
    assert not materialise.comfyui_workflow_replaced(image, edit_document=None)
    assert changed_facts is not None
    assert "prompt" not in changed_facts.chunks and "workflow" not in changed_facts.chunks
    assert 'livebound: {"generator": "ComfyUI"}' in changed_facts.chunks["parameters"]
    assert restored_facts is not None
    assert restored_facts.chunks["prompt"] == raw
    assert restored_facts.chunks["workflow"] == workflow


def test_comfyui_global_exclusions_use_the_standard_block(tmp_path):
    from backend.metadata import comfyui

    raw = json.dumps(
        {
            "Clip": {
                "class_type": "CLIPSetLastLayer",
                "inputs": {"stop_at_clip_layer": ["ClipValue", 0]},
            },
            "ClipValue": {"class_type": "Primitive", "inputs": {"Value": -2}},
        }
    )
    source = tmp_path / "excluded.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", raw)
    Image.new("RGB", (64, 64)).save(source, "PNG", pnginfo=info)
    image = {
        "id": None,
        "source_path": str(source),
        "raw_infotext": raw,
        "parsed": comfyui.parse(raw),
    }
    db.set_json_setting(materialise.EXCLUDED_FIELDS_SETTING, ["Clip skip"])

    target = materialise.path_for(image, tmp_path, materialise.upload_infotext(image))
    output = png_io.read(target)

    assert materialise.comfyui_workflow_replaced(image)
    assert output is not None
    assert "prompt" not in output.chunks
    assert "-2" not in "\n".join(_container_metadata_values(target))


def test_comfyui_prompt_exclusions_use_the_standard_block(tmp_path):
    from backend.metadata import comfyui

    raw = json.dumps(
        {
            "Positive": {
                "class_type": "CLIPTextEncode",
                "inputs": {"text": "scene REMOVE164R5"},
            },
            "Sampler": {
                "class_type": "KSampler",
                "inputs": {"positive": ["Positive", 0], "seed": 7, "steps": 20, "cfg": 5},
            },
        }
    )
    workflow = '{"nodes":[{"id":"Positive","widgets_values":["scene REMOVE164R5"]}]}'
    source = tmp_path / "prompt-excluded.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", raw)
    info.add_text("workflow", workflow)
    Image.new("RGB", (64, 64)).save(source, "PNG", pnginfo=info)
    image = {
        "id": None,
        "source_path": str(source),
        "raw_infotext": raw,
        "parsed": comfyui.parse(raw),
    }
    db.set_json_setting(materialise.PROMPT_EXCLUSIONS_SETTING, ["REMOVE164R5"])

    target = materialise.path_for(image, tmp_path, materialise.upload_infotext(image))
    output = png_io.read(target)

    assert materialise.comfyui_workflow_replaced(image)
    assert output is not None
    assert "prompt" not in output.chunks and "workflow" not in output.chunks
    assert "REMOVE164R5" not in "\n".join(_container_metadata_values(target))


def test_comfyui_resource_restore_keeps_the_original_graph_bytes(monkeypatch, tmp_path):
    from backend.metadata import comfyui

    raw = json.dumps(
        {
            "Positive": {"class_type": "CLIPTextEncode", "inputs": {"text": "scene"}},
            "Sampler": {
                "class_type": "KSampler",
                "inputs": {"positive": ["Positive", 0], "seed": 7, "steps": 20, "cfg": 5},
            },
            "Lora": {
                "class_type": "LoraLoader",
                "inputs": {
                    "lora_name": "detail.safetensors",
                    "strength_model": 0.65,
                    "strength_clip": 0.65,
                },
            },
        }
    )
    workflow = '{"nodes":[{"id":"Lora","widgets_values":["detail.safetensors",0.65,0.65]}]}'
    source = tmp_path / "restore-resource.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", raw)
    info.add_text("workflow", workflow)
    Image.new("RGB", (64, 64)).save(source, "PNG", pnginfo=info)
    image = {
        "id": 1,
        "source_path": str(source),
        "raw_infotext": raw,
        "parsed": comfyui.parse(raw),
    }
    row = {"resource_type": "lora", "name_in_prompt": "detail", "weight": 0.65}
    monkeypatch.setattr(
        image_store, "resources_for", lambda _image_id: [{**row, "deleted_by_user": 1}]
    )
    assert materialise.comfyui_workflow_replaced(image)

    monkeypatch.setattr(image_store, "resources_for", lambda _image_id: [row])
    target = materialise.path_for(image, tmp_path, materialise.upload_infotext(image))
    output = png_io.read(target)

    assert not materialise.comfyui_workflow_replaced(image)
    assert output is not None
    assert output.chunks["prompt"] == raw
    assert output.chunks["workflow"] == workflow


def test_civitai_request_projection_updates_extra_metadata(tmp_path):
    from test_metadata_families import CIVITAI_GRAPH

    from backend.metadata import civitai_generated

    source = tmp_path / "source.png"
    info = PngImagePlugin.PngInfo()
    info.add_text("prompt", CIVITAI_GRAPH)
    Image.new("RGB", (64, 64)).save(source, "PNG", pnginfo=info)
    image = {
        "id": None,
        "source_path": str(source),
        "raw_infotext": CIVITAI_GRAPH,
        "parsed": civitai_generated.parse(CIVITAI_GRAPH),
    }
    text = materialise.upload_infotext(
        image,
        edit_document={
            "draft": {"prompt": "new CivitAI request", "fields": {"Seed": 51515151}},
            "touched": ["prompt", "Seed"],
            "deleted": [],
        },
    )
    target = materialise.path_for(image, tmp_path, text)
    payload = "\n".join(_container_metadata_values(target))

    assert "a CivitAI request" not in payload
    assert "1311178179" not in payload
    assert "new CivitAI request" in payload
    assert "51515151" in payload


def test_a_fooocus_row_still_builds_an_a1111_block():
    from test_metadata_families import FOOOCUS

    projected = infotext.parse(materialise.upload_infotext({"id": None, "raw_infotext": FOOOCUS}))
    assert projected["prompt"] == "cinematic still a cat . moody, a cat, highly detailed"


def _is_image_meta_on_site(meta: dict) -> bool:
    if "civitaiResources" not in meta or "Version" in meta:
        return False
    model = meta.get("Model")
    return model is None or (isinstance(model, str) and model.startswith("urn:air:"))
