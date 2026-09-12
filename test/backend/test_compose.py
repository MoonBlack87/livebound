"""Rendering an edited infotext back out.

The property that matters: a field nobody touched comes out exactly as the
generator wrote it. Re-rendering everything from decoded values would change
fields nobody asked to change, and CivitAI reads what it is given.
"""

from __future__ import annotations

import pytest

from backend.metadata import compose, infotext

SAMPLE = (
    "a cat sitting on a wall, masterpiece, <lora:MoonTastic:0.8>\n"
    "Negative prompt: blurry, low quality\n"
    'Steps: 30, Sampler: DPM++ 2M, CFG scale: 7.0, Seed: 972684115, Size: 832x1216, '
    'Model hash: abc1234567, Model: MoonArtCauldron, '
    'Lora hashes: "MoonTastic: 1a2b3c4d5e6f", Version: f2.0.1'
)


def _edit(draft, touched=(), deleted=()):
    return {"draft": draft, "touched": list(touched), "deleted": list(deleted)}


def test_an_empty_edit_reproduces_the_text_byte_for_byte():
    assert compose.apply_edit(SAMPLE, None) == SAMPLE
    assert compose.apply_edit(SAMPLE, _edit({})) == SAMPLE


def test_an_untouched_field_keeps_its_original_spelling():
    """`Lora hashes` is written as quoted JSON; re-rendering it must not change that."""
    out = compose.apply_edit(SAMPLE, _edit({"prompt": "something else"}, ["prompt"]))

    assert 'Lora hashes: "MoonTastic: 1a2b3c4d5e6f"' in out
    assert out.startswith("something else\n")
    assert infotext.parse(out)["fields"]["Seed"] == 972684115


def test_a_changed_field_is_rendered_and_the_rest_stays():
    out = compose.apply_edit(SAMPLE, _edit({"fields": {"Steps": 45}}, ["Steps"]))

    parsed = infotext.parse(out)
    assert parsed["fields"]["Steps"] == 45
    assert parsed["fields"]["Sampler"] == "DPM++ 2M"
    assert parsed["prompt"] == infotext.parse(SAMPLE)["prompt"]


def test_a_deleted_field_disappears():
    out = compose.apply_edit(SAMPLE, _edit({}, deleted=["Model hash"]))

    fields = infotext.parse(out)["fields"]
    assert "Model hash" not in fields
    assert fields["Model"] == "MoonArtCauldron", "its neighbour is untouched"


def test_a_new_field_is_appended():
    out = compose.apply_edit(SAMPLE, _edit({"fields": {"Clip skip": 2}}, ["Clip skip"]))

    assert out.rstrip().endswith("Clip skip: 2")
    assert infotext.parse(out)["fields"]["Clip skip"] == 2


def test_the_negative_prompt_can_be_removed_entirely():
    out = compose.apply_edit(SAMPLE, _edit({}, deleted=["negative_prompt"]))

    assert infotext.parse(out)["negative_prompt"] is None
    assert "Negative prompt:" not in out


@pytest.mark.parametrize(
    "value,expected",
    [
        (42, "42"),
        (7.5, "7.5"),
        ("plain", "plain"),
        ("has, a comma", '"has, a comma"'),
        (["a", "b"], '["a", "b"]'),
    ],
)
def test_values_are_rendered_the_way_the_parser_reads_them(value, expected):
    assert compose.render(value) == expected


def test_structured_values_escape_non_latin1_characters():
    rendered = compose.render({"modelName": "🐻"})

    assert rendered == '{"modelName": "\\ud83d\\udc3b"}'
    assert rendered.encode("latin-1")


def test_a_value_with_a_comma_survives_a_round_trip():
    """Unquoted it would be read as the start of the next key."""
    out = compose.apply_edit(SAMPLE, _edit({"fields": {"Notes": "one, two"}}, ["Notes"]))

    assert infotext.parse(out)["fields"]["Notes"] == "one, two"


def test_an_image_without_a_parameter_line_still_takes_an_edit():
    out = compose.apply_edit("just a prompt", _edit({"prompt": "a better prompt"}, ["prompt"]))

    assert out == "a better prompt"
