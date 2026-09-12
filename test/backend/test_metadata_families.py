"""Three generator families share one PNG chunk, so detection has to be exact.

A1111, SwarmUI and RuinedFooocus all write into ``parameters``. Before these
tests the A1111 adapter claimed anything found there, so a SwarmUI image was
parsed as an infotext and its entire JSON document became the prompt - with no
warning, and pushed to CivitAI that way. What is asserted here is therefore not
that each family can be read, but that no family takes another's images.
"""

from __future__ import annotations

from backend.metadata import hfspace, invokeai, png_io, ruinedfooocus, swarmui, tensorart

SWARMUI = (
    '{"sui_image_params": {"prompt": "a cat on a roof", "negativeprompt": "blurry",'
    ' "model": "sdxl_base", "seed": 12345, "steps": 30, "cfgscale": 7.0,'
    ' "sampler": "euler", "scheduler": "karras", "width": 1024, "height": 1536},'
    ' "sui_models": [{"name": "detail.safetensors", "param": "loras", "hash": "0xabc"}]}'
)

#: Shaped after a real file the maintainer supplied - two years old, and the
#: format held. Its `loras` entry packs a hash with a weight and a Windows path
#: in one string; CivitAI's own parser leaves that key untouched, which is why
#: such an image arrives there with its prompt and without its resources.
RUINED_FOOOCUS = (
    '{"Prompt": "a dog", "Negative": "ugly", "steps": 24, "cfg": 4.5, "seed": 7,'
    ' "sampler_name": "dpmpp_2m", "scheduler": "simple",'
    ' "base_model_name": "juggernaut.safetensors", "base_model_hash": "abc123",'
    ' "loras": [["BFA3BB4FBE", "1.0 - moi\\\\helios-beasts.safetensors"]],'
    ' "software": "RuinedFooocus"}'
)

A1111 = (
    "a cat\nNegative prompt: blurry\n"
    "Steps: 30, Sampler: DPM++ 2M, CFG scale: 7, Seed: 12345, Size: 1024x1024, Model: sdxl_base"
)

#: Cut from ``comfyui.png`` in the reference corpus. The graph is the API graph,
#: not the editor workflow; every node here contributes one fact the reader uses.
COMFY_GRAPH = (
    '{"CheckpointLoader_Base":{"inputs":{"ckpt_name":"waiIllustriousSDXL_v160.safetensors"},'
    '"class_type":"CheckpointLoaderSimple"},'
    '"EmptyLatentImage":{"inputs":{"width":1024,"height":1024},'
    '"class_type":"EmptyLatentImage"},'
    '"Positive":{"inputs":{"text_g":"a real corpus prompt","text_l":"more detail"},'
    '"class_type":"CLIPTextEncode"},'
    '"Negative":{"inputs":{"text":"bad quality"},"class_type":"CLIPTextEncode"},'
    '"CLIP_Skip_Base":{"inputs":{"stop_at_clip_layer":-2},"class_type":"CLIPSetLastLayer"},'
    '"LoraActive":{"inputs":{"lora_name":"detail.safetensors","strength_model":0.65},'
    '"class_type":"LoraLoader"},'
    '"LoraOff":{"inputs":{"lora_name":"disabled.safetensors","strength_model":0},'
    '"class_type":"LoraLoaderModelOnly"},'
    '"Sampler":{"inputs":{"seed":["Seed",0],"steps":20,"cfg":5.0,'
    '"sampler_name":"euler_ancestral","scheduler":"karras","denoise":1.0,'
    '"positive":["Positive",0],"negative":["Negative",0],'
    '"latent_image":["EmptyLatentImage",0]},"class_type":"KSampler"},'
    '"Seed":{"inputs":{"Value":422847914351919},"class_type":"Primitive"}}'
)

#: Cut from ``civitai-hires.jpg``. The site's request lives alongside its graph,
#: and the AIRs identify the model versions without a hash lookup.
CIVITAI_GRAPH = (
    '{"resource-stack":{"class_type":"CheckpointLoaderSimple",'
    '"inputs":{"ckpt_name":"urn:air:sdxl:checkpoint:civitai:827184@2514310"}},'
    '"extra":{"airs":["urn:air:sdxl:checkpoint:civitai:827184@2514310",'
    '"urn:air:other:upscaler:civitai:147759@164821"]},'
    '"extraMetadata":"{\\"prompt\\":\\"a CivitAI request\\",'
    '\\"negativePrompt\\":\\"bad\\",\\"cfgScale\\":5,\\"sampler\\":\\"euler_ancestral\\",'
    '\\"clipSkip\\":2,\\"steps\\":25,\\"seed\\":1311178179,\\"width\\":1024,'
    '\\"height\\":1024,\\"baseModel\\":\\"Illustrious\\",'
    '\\"resources\\":[{\\"modelVersionId\\":2514310,\\"strength\\":1}]}"}'
)


def _family(text: str) -> str:
    return png_io.detect_metadata_family({"parameters": text}, None)


def test_each_family_recognises_only_its_own():
    assert _family(SWARMUI) == "swarmui"
    assert _family(RUINED_FOOOCUS) == "ruinedfooocus"
    assert _family(A1111) == "a1111"
    assert png_io.detect_metadata_family({"prompt": COMFY_GRAPH}, None) == "comfyui"
    assert png_io.detect_metadata_family({}, CIVITAI_GRAPH) == "civitai"


def test_a1111_no_longer_claims_json_it_cannot_read():
    """The defect this file exists for: the whole document became the prompt."""
    from backend.metadata import infotext

    assert not infotext.detects({"parameters": SWARMUI}, None)
    assert not infotext.detects({"parameters": RUINED_FOOOCUS}, None)
    assert not infotext.detects({"parameters": COMFY_GRAPH}, None)
    assert infotext.detects({"parameters": A1111}, None)


def test_swarmui_lands_in_the_vocabulary_we_already_have():
    parsed = swarmui.read({"parameters": SWARMUI}, None)
    assert parsed["prompt"] == "a cat on a roof"
    assert parsed["negative_prompt"] == "blurry"
    assert parsed["fields"] == {
        "Steps": 30,
        "Sampler": "euler",
        "Schedule type": "karras",
        "CFG scale": 7.0,
        "Seed": 12345,
        "Model": "sdxl_base",
        "Size": "1024x1536",
        # from sui_models, in the spelling `resources` already reads
        "Lora hashes": "detail: abc",
    }


def test_ruinedfooocus_drops_the_extension_and_the_default_scheduler():
    parsed = ruinedfooocus.read({"parameters": RUINED_FOOOCUS}, None)
    assert parsed["prompt"] == "a dog"
    assert parsed["negative_prompt"] == "ugly"
    # `juggernaut.safetensors` and `juggernaut` would otherwise be two models,
    # and `simple` is what it writes when nothing was chosen.
    assert parsed["fields"]["Model"] == "juggernaut"
    assert "Schedule type" not in parsed["fields"]
    assert parsed["fields"]["Model hash"] == "abc123"


def test_a_prompt_that_opens_with_a_brace_is_still_a1111():
    """A1111 prompts may start with `{` - dynamic prompts do exactly that."""
    text = "{red|blue} car\nSteps: 20, Sampler: Euler, CFG scale: 7"
    assert _family(text) == "a1111"


def test_an_empty_or_absent_chunk_belongs_to_nobody():
    assert _family("") == png_io.UNKNOWN_METADATA_FAMILY
    assert png_io.detect_metadata_family({}, None) == png_io.UNKNOWN_METADATA_FAMILY


def test_comfyui_graph_reads_the_existing_field_vocabulary_and_active_lora_only(monkeypatch):
    """The graph's evidence, not a prompt tag, creates the named LoRA row."""
    from backend.metadata import comfyui, resources
    from backend.store import model_roots

    parsed = comfyui.parse(COMFY_GRAPH)
    assert parsed["prompt"] == "a real corpus prompt, more detail"
    assert parsed["negative_prompt"] == "bad quality"
    assert parsed["fields"] == {
        "Steps": 20,
        "CFG scale": 5.0,
        "Sampler": "euler_ancestral",
        "Schedule type": "karras",
        "Denoising strength": 1.0,
        "Seed": 422847914351919,
        "Size": "1024x1024",
        "Model": "waiIllustriousSDXL_v160",
        "Clip skip": 2,
        "Lora weights": "detail: 0.65",
    }

    monkeypatch.setattr(
        model_roots, "hash_for_prompt_tag", lambda name: "A" * 64 if name == "detail" else None
    )
    rows = resources.extract(parsed)
    assert [
        (row["resource_type"], row["name_in_prompt"], row["hash"], row["weight"]) for row in rows
    ] == [
        ("checkpoint", "waiIllustriousSDXL_v160", None, None),
        ("lora", "detail", "A" * 64, 0.65),
    ]


def test_civitai_graph_uses_request_values_and_embedded_version_id():
    from backend.metadata import civitai_generated, resources

    parsed = civitai_generated.parse(CIVITAI_GRAPH)
    assert parsed["prompt"] == "a CivitAI request"
    assert parsed["negative_prompt"] == "bad"
    assert parsed["fields"]["Civitai resources"] == [
        {"type": "checkpoint", "modelVersionId": 2514310, "weight": 1},
        {"type": "upscaler", "modelVersionId": 164821},
    ]
    # A model version id is not a generator version, and `Version` is the field
    # CivitAI reads as "made with external tooling" (`CVT-15`). Writing one there
    # would state a fact the file does not contain, and would go out with the
    # upload; the id belongs in the credits, where it identifies a model.
    assert "Version" not in parsed["fields"]
    assert "Model" not in parsed["fields"]
    assert [row["model_version_id"] for row in resources.extract(parsed)] == [2514310, 164821]


def test_swarmui_resources_arrive_as_hashes_that_can_actually_match():
    """The one thing SwarmUI gives us that A1111 cannot.

    A1111 writes a LoRA hash taken over the tensor data, which never equals the
    SHA256 this application stores for a local file - 186 rows in the
    maintainer's library are unresolved for exactly that reason (task 154).
    SwarmUI writes the file's own SHA256, prefixed with `0x`. Mapped onto the
    field names `resources` already reads, such an image resolves by hash with
    nothing added there.
    """
    parsed = swarmui.read({"parameters": SWARMUI}, None)
    assert parsed["fields"]["Lora hashes"] == "detail: abc"
    assert "Model hash" not in parsed["fields"]  # this sample names no checkpoint hash

    with_model = SWARMUI.replace(
        '"param": "loras", "hash": "0xabc"',
        '"param": "loras", "hash": "0xabc"}, {"name": "base.safetensors",'
        ' "param": "model", "hash": "0xDEF"',
    )
    parsed = swarmui.read({"parameters": with_model}, None)
    assert parsed["fields"]["Model hash"] == "DEF"


def test_a_lora_keeps_its_name_and_loses_the_folder_it_was_found_in():
    """`Krea 2/Characters/EvelynK2.safetensors` is one machine's arrangement."""
    document = SWARMUI.replace('"name": "detail.safetensors"', '"name": "a/b/detail.safetensors"')
    parsed = swarmui.read({"parameters": document}, None)
    assert parsed["fields"]["Lora hashes"] == "detail: abc"


def test_ruinedfooocus_reads_the_resources_civitai_leaves_behind():
    """Two values in one string, and a path from somebody else's machine."""
    parsed = ruinedfooocus.read({"parameters": RUINED_FOOOCUS}, None)
    assert parsed["fields"]["Lora hashes"] == "helios-beasts: BFA3BB4FBE"
    assert parsed["fields"]["Model hash"] == "abc123"


def test_ruinedfooocus_tolerates_the_older_written_form():
    """Some writers put Python's `str` of the list there instead of an array."""
    document = RUINED_FOOOCUS.replace(
        '"loras": [["BFA3BB4FBE", "1.0 - moi\\\\helios-beasts.safetensors"]]',
        '"loras": "[[\'BFA3BB4FBE\', \'1.0 - moi/helios-beasts.safetensors\']]"',
    )
    parsed = ruinedfooocus.read({"parameters": document}, None)
    assert parsed["fields"]["Lora hashes"] == "helios-beasts: BFA3BB4FBE"


def test_an_unreadable_loras_field_is_skipped_not_announced():
    """The sample is two years old; a shape we do not know is not an error."""
    document = RUINED_FOOOCUS.replace(
        '"loras": [["BFA3BB4FBE", "1.0 - moi\\\\helios-beasts.safetensors"]]',
        '"loras": {"unexpected": "shape"}',
    )
    parsed = ruinedfooocus.read({"parameters": document}, None)
    assert "Lora hashes" not in parsed["fields"]
    assert parsed["warnings"] == []


# --- the four families added from a corpus of 93 real files -------------------
#
# Payloads are shaped after what those files actually contain, written out here
# rather than copied in: the corpus is MIT-licensed and lives outside this
# repository, so nothing of it ships and nothing has to be attributed.

NOVELAI_COMMENT = (
    '{"prompt": "1girl, solo", "uc": "nsfw, lowres", "steps": 28, "scale": 5.0,'
    ' "seed": 2043807047, "sampler": "k_euler_ancestral", "noise_schedule": "karras",'
    ' "width": 832, "height": 1216}'
)
NOVELAI_CHUNKS = {
    "Software": "NovelAI",
    "Source": "NovelAI Diffusion V4.5 4BDE2A90",
    "Description": "1girl, solo",
    "Comment": NOVELAI_COMMENT,
}

INVOKEAI = (
    '{"generation_mode": "sdxl_txt2img", "positive_prompt": "a cat",'
    ' "negative_prompt": "bad quality", "width": 1024, "height": 1024,'
    ' "seed": 3951921344, "cfg_scale": 6.0, "steps": 24, "scheduler": "euler_a",'
    ' "model": {"key": "179d02bf", "hash": "blake3:7c1631b4", "name": "wai_v16"}}'
)

HF_SPACE = (
    '{"prompt": "a cat", "negative_prompt": "bad quality", "resolution": "1024 x 1024",'
    ' "guidance_scale": 5.5, "num_inference_steps": 24, "seed": 443800072,'
    ' "sampler": "DPM++ 2M Karras", "Model": "WAI v16", "Model hash": "BDB59BAC77"}'
)

#: TensorArt writes a trailing NUL after its document.
TENSORART = (
    '{"prompt": "a cat", "negativePrompt": "bad quality", "width": 1024, "height": 1024,'
    ' "steps": 25, "cfgScale": 7, "seed": -1, "clipSkip": 2, "ksamplerName": "euler_ancestral",'
    ' "schedule": "karras", "baseModel": {"label": "v16", "modelId": "943946051"}}\x00'
)


def test_novelai_is_read_from_its_own_chunks():
    assert png_io.detect_metadata_family(NOVELAI_CHUNKS, None) == "novelai"
    parsed = png_io.read_metadata(
        png_io.ImageFacts(0, 0, "image/png", chunks=dict(NOVELAI_CHUNKS), metadata_family="novelai")
    )
    assert parsed["prompt"] == "1girl, solo"
    assert parsed["negative_prompt"] == "nsfw, lowres"
    assert parsed["fields"]["CFG scale"] == 5.0  # NovelAI calls it `scale`
    assert parsed["fields"]["Schedule type"] == "karras"
    assert parsed["fields"]["Size"] == "832x1216"
    # `NovelAI Diffusion V4.5 4BDE2A90` is a name and a hash in one string.
    assert parsed["fields"]["Model"] == "NovelAI Diffusion V4.5"
    assert parsed["fields"]["Model hash"] == "4BDE2A90"


def test_novelai_in_a_jpeg_arrives_wrapped_in_an_envelope():
    """EXIF has one field, so the document travels inside `{"Comment": "..."}`."""
    import json

    envelope = json.dumps({"Comment": NOVELAI_COMMENT})
    assert png_io.detect_metadata_family({}, envelope) == "novelai"


def test_invokeai_keeps_the_model_name_and_drops_a_hash_that_cannot_match():
    assert png_io.detect_metadata_family({"invokeai_metadata": INVOKEAI}, None) == "invokeai"
    parsed = invokeai.read({"invokeai_metadata": INVOKEAI}, None)
    assert parsed["prompt"] == "a cat"
    assert parsed["fields"]["Sampler"] == "euler_a"
    assert parsed["fields"]["Model"] == "wai_v16"
    # blake3 is not what any hash here is compared against; recording it as a
    # `Model hash` would make a row that can never resolve.
    assert "Model hash" not in parsed["fields"]


def test_the_hugging_face_shape_is_claimed_by_shape_since_it_names_nobody():
    assert png_io.detect_metadata_family({"parameters": HF_SPACE}, None) == "hf-space"
    parsed = hfspace.read({"parameters": HF_SPACE}, None)
    assert parsed["fields"]["Steps"] == 24
    assert parsed["fields"]["CFG scale"] == 5.5
    assert parsed["fields"]["Size"] == "1024x1024"  # from "1024 x 1024"


def test_tensorart_reads_past_the_trailing_nul():
    assert png_io.detect_metadata_family({"generation_data": TENSORART}, None) == "tensorart"
    parsed = tensorart.read({"generation_data": TENSORART}, None)
    assert parsed["prompt"] == "a cat"
    assert parsed["fields"]["Clip skip"] == 2
    assert parsed["fields"]["Model"] == "v16"


def test_no_family_claims_a_file_that_carries_nothing():
    """GIMP writes a `Comment`, and an empty file writes nothing at all."""
    assert png_io.detect_metadata_family({"Comment": "Created with GIMP"}, None) == (
        png_io.UNKNOWN_METADATA_FAMILY
    )
    assert png_io.detect_metadata_family({}, None) == png_io.UNKNOWN_METADATA_FAMILY


def test_a_stated_weight_travels_from_two_parallel_lists_to_the_resource_row():
    """SwarmUI keeps the weight away from the hash, in a second list.

    `sui_models` has the digest, `loras`/`loraweights` under the parameters have
    the strength, matched by position and carrying the folder the file was found
    in. Neither list alone is a resource row, and A1111's `<lora:name:0.8>` tag
    - the only weight this application used to read - is never written here.
    """
    from backend.metadata import resources

    document = SWARMUI.replace(
        '"height": 1536',
        '"height": 1536, "loras": ["style/detail"], "loraweights": ["0.65"]',
    )
    parsed = swarmui.read({"parameters": document}, None)
    assert parsed["fields"]["Lora weights"] == "detail: 0.65"

    row = next(
        entry
        for entry in resources.extract(parsed)
        if entry["name_in_prompt"] == "detail"
    )
    assert row["hash"] == "ABC"
    assert row["weight"] == 0.65


def test_a_weight_without_its_partner_is_left_alone():
    """One list without the other says nothing, and half a pair says less."""
    document = SWARMUI.replace('"height": 1536', '"height": 1536, "loraweights": ["0.65"]')
    assert "Lora weights" not in swarmui.read({"parameters": document}, None)["fields"]


def test_ruinedfooocus_splits_the_weight_out_of_the_packed_string():
    """`"1.0 - moi\\helios-beasts.safetensors"` is weight, separator and path."""
    from backend.metadata import resources

    parsed = ruinedfooocus.read({"parameters": RUINED_FOOOCUS}, None)
    assert parsed["fields"]["Lora weights"] == "helios-beasts: 1.0"

    row = next(
        entry
        for entry in resources.extract(parsed)
        if entry["name_in_prompt"] == "helios-beasts"
    )
    assert (row["hash"], row["weight"]) == ("BFA3BB4FBE", 1.0)


# --- Fooocus and the fork of it ----------------------------------------------

#: The document Fooocus writes when its metadata scheme is `fooocus`. Shaped
#: after `fooocus-meta.png` in fooocus-metadata's own testdata, with the two
#: long prompts cut short.
FOOOCUS = (
    '{"adm_guidance": "(1.5, 0.8, 0.3)", "base_model": "juggernautXL_v8Rundiffusion",'
    ' "base_model_hash": "aeb7e9e689", "clip_skip": 2,'
    ' "full_negative_prompt": ["blurry", "watermark"],'
    ' "full_prompt": ["cinematic still a cat . moody", "a cat, highly detailed"],'
    ' "guidance_scale": 4, "lora_combined_1": "sd_xl_offset_example-lora_1.0 : 0.1",'
    ' "loras": [["sd_xl_offset_example-lora_1.0", 0.1, "4852686128"]],'
    ' "metadata_scheme": "fooocus", "negative_prompt": "", "performance": "Speed",'
    ' "prompt": "a cat", "refiner_model": "None", "refiner_switch": 0.5,'
    ' "resolution": "(512, 512)", "sampler": "dpmpp_2m_sde_gpu", "scheduler": "karras",'
    ' "seed": "127589946317439009", "sharpness": 2, "steps": 30,'
    ' "styles": "[\'Fooocus V2\']", "vae": "Default (model)", "version": "Fooocus v2.5.5"}'
)

#: The same picture, saved with the scheme set to `a1111` instead. Every value
#: is the one Fooocus itself writes there - including the two field names it
#: gives the LoRA, which is where this application took them from.
FOOOCUS_AS_A1111 = (
    "cinematic still a cat . moody, a cat, highly detailed\n"
    "Negative prompt: blurry, watermark\n"
    "Steps: 30, Sampler: DPM++ 2M SDE Karras, Seed: 127589946317439009, Size: 512x512,"
    ' CFG scale: 4, Sharpness: 2, ADM Guidance: "(1.5, 0.8, 0.3)",'
    " Model: juggernautXL_v8Rundiffusion, Model hash: aeb7e9e689, Performance: Speed,"
    " Scheduler: karras, VAE: Default (model), Raw prompt: a cat, Raw negative prompt: ,"
    ' Clip skip: 2, Lora hashes: "sd_xl_offset_example-lora_1.0: 4852686128",'
    ' Lora weights: "sd_xl_offset_example-lora_1.0: 0.1", Version: Fooocus v2.5.5'
)

#: FooocusPlus keeps the parent's document and title-cases every key. Its own
#: name is the only thing in it that the parent never writes.
FOOOCUS_PLUS = (
    '{"ADM Guidance": "(1.5, 0.8, 0.3)", "Backend Engine": "SDXL-Fooocus",'
    ' "Base Model": "elsewhereXL_v10", "Base Model Hash": "79fd29ab43", "CLIP Skip": 2,'
    ' "Full Negative Prompt": ["blurry"],'
    ' "Full Prompt": ["a cat", "a cat, beautiful dramatic atmosphere"],'
    ' "Guidance Scale": 4.5, "LoRAs": [], "Metadata Scheme": "Fooocus",'
    ' "Negative Prompt": "", "Performance": "Speed", "Prompt": "a cat",'
    ' "Refiner Model": "None", "Refiner Switch": 0.6, "Resolution": "(1024, 1024)",'
    ' "Sampler": "dpmpp_2m_sde_gpu", "Scheduler": "karras", "Seed": "5256010854089202552",'
    ' "Sharpness": 6, "Steps": 30, "User": "FooocusPlus", "VAE": "Default (model)",'
    ' "Version": "FooocusPlus 1.0.0"}'
)


def test_the_fooocus_family_and_its_fork_do_not_take_each_other():
    assert _family(FOOOCUS) == "fooocus"
    assert _family(FOOOCUS_PLUS) == "fooocusplus"
    # The fork's own chunk, and the one an image editor uses for something else.
    assert png_io.detect_metadata_family({"Comment": FOOOCUS_PLUS}, None) == "fooocusplus"
    assert png_io.detect_metadata_family({"Comment": "Created with GIMP"}, None) == (
        png_io.UNKNOWN_METADATA_FAMILY
    )
    # Both write into EXIF in a JPEG or a WebP, where there is no chunk at all.
    assert png_io.detect_metadata_family({}, FOOOCUS) == "fooocus"
    assert png_io.detect_metadata_family({}, FOOOCUS_PLUS) == "fooocusplus"
    # And the same generator's other scheme stays with the adapter that reads it.
    assert _family(FOOOCUS_AS_A1111) == "a1111"


def test_both_fooocus_schemes_describe_the_same_picture_the_same_way():
    """The point of reading the JSON scheme at all.

    Which of the two Fooocus writes is a checkbox in its settings, and the user
    who ticked it did not mean to publish a different prompt or lose a LoRA. The
    prompt, the negative prompt and the resources must therefore come out
    identical - the expanded prompt among them, because that is the one Fooocus
    puts in the a1111 block.
    """
    from backend.metadata import resources

    json_scheme = png_io.read_metadata(
        png_io.ImageFacts(1, 1, "image/png", chunks={"parameters": FOOOCUS},
                          metadata_family="fooocus")
    )
    a1111_scheme = png_io.read_metadata(
        png_io.ImageFacts(1, 1, "image/png", chunks={"parameters": FOOOCUS_AS_A1111},
                          metadata_family="a1111")
    )

    assert json_scheme["prompt"] == a1111_scheme["prompt"]
    assert json_scheme["negative_prompt"] == a1111_scheme["negative_prompt"]

    def rows(parsed):
        return [
            (row["resource_type"], row["name_in_prompt"], row["hash"], row["weight"])
            for row in resources.extract(parsed)
        ]

    assert rows(json_scheme) == rows(a1111_scheme)
    assert rows(json_scheme) == [
        ("checkpoint", "juggernautXL_v8Rundiffusion", "AEB7E9E689", None),
        ("lora", "sd_xl_offset_example-lora_1.0", "4852686128", 0.1),
    ]

    # Two names are knowingly not the same. `Scheduler` is `Schedule type` here
    # for every family; the sampler keeps whichever name its file states, since
    # translating one into A1111's display name is a table, not a reading.
    shared = set(json_scheme["fields"]) & set(a1111_scheme["fields"])
    differing = {
        name
        for name in shared
        if str(json_scheme["fields"][name]) != str(a1111_scheme["fields"][name])
    }
    assert differing == {"Sampler"}
    assert "Schedule type" in json_scheme["fields"]
    assert "Scheduler" in a1111_scheme["fields"]


def test_a_refiner_that_was_not_used_is_not_a_field():
    """`"refiner_model": "None"` is the absence of one, written down."""
    from backend.metadata import fooocus

    parsed = fooocus.parse(FOOOCUS)
    assert "Refiner model" not in parsed["fields"]
    assert "Refiner switch" not in parsed["fields"]

    used = FOOOCUS.replace('"refiner_model": "None"', '"refiner_model": "juggernaut_refiner"')
    parsed = fooocus.parse(used)
    assert parsed["fields"]["Refiner model"] == "juggernaut_refiner"
    assert parsed["fields"]["Refiner switch"] == 0.5


def test_the_fork_says_the_subject_once():
    """With no styles chosen, `Full Prompt` is the prompt and an expansion of it.

    Joining both the way Fooocus does would open every such image with its own
    subject twice.
    """
    from backend.metadata import fooocusplus

    parsed = fooocusplus.parse(FOOOCUS_PLUS)
    assert parsed["prompt"] == "a cat, beautiful dramatic atmosphere"
    assert parsed["fields"]["Raw prompt"] == "a cat"
    assert parsed["fields"]["Backend engine"] == "SDXL-Fooocus"
    assert parsed["fields"]["Model"] == "elsewhereXL_v10"
    assert parsed["fields"]["Size"] == "1024x1024"


def test_a_lora_from_before_the_hash_was_written_still_becomes_a_named_row():
    """Fooocus v2.2 numbered its LoRAs and gave no hash for any of them.

    Its name and weight are generator evidence. It remains an unresolved row
    when no local file supplies a hash, so the user can bind it by hand.
    """
    from backend.metadata import fooocus, resources

    document = FOOOCUS.replace(
        '"loras": [["sd_xl_offset_example-lora_1.0", 0.1, "4852686128"]], ', ""
    )
    parsed = fooocus.parse(document)
    assert "Lora hashes" not in parsed["fields"]
    assert parsed["fields"]["Lora weights"] == "sd_xl_offset_example-lora_1.0: 0.1"
    rows = resources.extract(parsed)
    assert [
        (row["resource_type"], row["name_in_prompt"], row["hash"], row["weight"]) for row in rows
    ] == [
        ("checkpoint", "juggernautXL_v8Rundiffusion", "AEB7E9E689", None),
        ("lora", "sd_xl_offset_example-lora_1.0", None, 0.1),
    ]


def test_a_summary_without_civitai_ids_is_still_an_ordinary_graph():
    """The site's family needs all three, not two.

    A save node may attach a request summary to any ComfyUI image. Claiming that
    one here would answer it from the summary and drop what only the graph holds
    — its checkpoint, its LoRAs, every field the summary does not repeat.
    """
    document = COMFY_GRAPH[:-1] + ',"extraMetadata":"{\\"prompt\\":\\"local\\"}"}'
    assert png_io.detect_metadata_family({"prompt": document}, None) == "comfyui"
    parsed = png_io.read_metadata(
        png_io.ImageFacts(1, 1, "image/png", chunks={"prompt": document},
                          metadata_family="comfyui")
    )
    assert parsed["fields"]["Model"] == "waiIllustriousSDXL_v160"
    assert png_io.detect_metadata_family({"prompt": CIVITAI_GRAPH}, None) == "civitai"


def test_one_picker_with_two_outputs_does_not_name_both_the_same():
    """A link is a node **and** an output slot, and dropping the slot invents.

    A picker that hands out a checkpoint on one output and an upscaler on the
    next has two names. Reading only its first would publish the checkpoint's
    identity as the upscaler's — a credit that is wrong rather than missing.
    """
    from backend.metadata import comfyui

    graph = (
        '{"Picker":{"inputs":{"value":["a-checkpoint","an-upscaler"]},'
        '"class_type":"SomeResourcePicker"},'
        '"Load":{"inputs":{"ckpt_name":["Picker",0]},"class_type":"CheckpointLoaderSimple"},'
        '"Up":{"inputs":{"model_name":["Picker",1]},"class_type":"UpscaleModelLoader"},'
        '"Sampler":{"inputs":{"steps":20,"cfg":6,"seed":1,"sampler_name":"euler",'
        '"scheduler":"normal","positive":["P",0],"negative":["N",0],'
        '"latent_image":["Latent",0],"model":["Load",0]},"class_type":"KSampler"},'
        '"Latent":{"inputs":{"width":512,"height":512},"class_type":"EmptyLatentImage"},'
        '"P":{"inputs":{"text":"x"},"class_type":"CLIPTextEncode"},'
        '"N":{"inputs":{"text":"y"},"class_type":"CLIPTextEncode"}}'
    )
    parsed = comfyui.parse(graph)
    assert parsed["fields"]["Model"] == "a-checkpoint"
    assert parsed["fields"]["Hires upscaler"] == "an-upscaler"

    # A node that explains no slot answers for none of them past the first.
    opaque = graph.replace('"value":["a-checkpoint","an-upscaler"]', '"value":"a-checkpoint"')
    parsed = comfyui.parse(opaque)
    assert parsed["fields"]["Model"] == "a-checkpoint"
    assert "Hires upscaler" not in parsed["fields"]


def test_an_image_civitai_generated_keeps_its_credits_and_gains_no_model():
    """What the maintainer decided on 2026-09-06, at the upload end.

    Such a file names no checkpoint — it has a model version id and nothing
    else. Withholding the credits to avoid CivitAI reading the upload as
    generated on its site would throw away the only identity the image has, to
    deny something that is true of it. Nothing is invented in exchange: no
    `Model`, no `Version`.
    """
    from backend.metadata import civitai_generated, compose, resources, upload_document

    parsed = civitai_generated.parse(CIVITAI_GRAPH)
    rows = resources.extract(parsed)
    text = "\n".join(
        (
            parsed["prompt"],
            f"Negative prompt: {parsed['negative_prompt']}",
            ", ".join(
                f"{key}: {compose.render(value)}" for key, value in parsed["fields"].items()
            ),
        )
    )
    meta = upload_document.render_meta(upload_document.build(text, rows))

    assert meta is not None
    assert "Model" not in meta and "Version" not in meta
    assert [entry["modelVersionId"] for entry in meta["civitaiResources"]] == [2514310, 164821]


def test_the_prompt_is_read_where_the_link_leads_not_where_it_looks_like_one():
    """Three corpus graphs keep their prompt in a custom node and nowhere else.

    rgthree's Power Prompt calls it `prompt`, Comfyroll's text box `Text`, a
    multiline primitive `value`. Each is reached by following the encoder's own
    wiring, which is why this is reading rather than guessing: a document search
    for something prompt-shaped would just as happily find an instruction meant
    for a language model.
    """
    from backend.metadata import comfyui

    graph = COMFY_GRAPH.replace(
        '"Negative":{"inputs":{"text":"bad quality"},"class_type":"CLIPTextEncode"}',
        '"Negative":{"inputs":{"text":["Box",0]},"class_type":"CLIPTextEncode"},'
        '"Box":{"inputs":{"Text":"bad quality"},"class_type":"DF_Text_Box"}',
    )
    assert comfyui.parse(graph)["negative_prompt"] == "bad quality"
