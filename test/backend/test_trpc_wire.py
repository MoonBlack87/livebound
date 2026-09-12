"""The two response formats CivitAI currently serves side by side."""

from __future__ import annotations

import pytest

from backend.civitai import trpc
from backend.civitai.errors import CivitaiError
from backend.civitai.trpc import _decode_devalue, _unwrap


def test_superjson_is_read():
    assert _unwrap({"result": {"data": {"json": {"id": 7}}}}, "post.get") == {"id": 7}


def test_the_flattened_format_is_read_too():
    """Measured against a real image.getInfinite response: the result arrived as a
    string, not an object - anyone reading only superjson passes the raw text
    through."""
    raw = '[{"nextCursor":-1,"items":1,"source":2},[],"db"]'
    assert _unwrap({"result": {"data": raw}}, "image.getInfinite") == {
        "nextCursor": None,
        "items": [],
        "source": "db",
    }


def test_shared_references_are_resolved_not_duplicated():
    """The point of the format: an identical sub-object appears only once."""
    raw = '[{"items":1},[2,3],{"id":4,"tags":5},{"id":6,"tags":5},11,[7],99,{"name":8}, "lora"]'
    result = _decode_devalue(raw)
    assert result == {
        "items": [
            {"id": 11, "tags": [{"name": "lora"}]},
            {"id": 99, "tags": [{"name": "lora"}]},
        ]
    }


def test_sentinels_become_none():
    assert _decode_devalue('[{"a":-1,"b":-2}]') == {"a": None, "b": None}


def test_a_cycle_does_not_hang():
    """The format allows cycles; a naive decoder runs forever in one."""
    assert _decode_devalue('[{"self":0}]') == {"self": None}


def test_unparsable_input_is_passed_through_rather_than_raising():
    assert _decode_devalue("not json") == "not json"


def test_civitais_deleted_post_wording_is_recognised_as_not_found(monkeypatch):
    """Measured 2026-08-22 on the deleted posts 30548631 and 30548684: CivitAI
    answers "Could not find entity", which matched neither phrase this used to
    look for. `operations.delete_remote` suppresses NotFound so that deleting an
    already-deleted post is a no-op - with a generic error it stopped being one."""
    from backend.civitai import mcp
    from backend.civitai.errors import NotFound

    monkeypatch.setattr(
        mcp.client,
        "post_json",
        lambda *args, **kwargs: {
            "result": {
                "isError": True,
                "content": [{"text": "Error: post.get: Could not find entity"}],
            }
        },
    )

    with pytest.raises(NotFound):
        mcp.call("get_post", {"postId": 1})


def test_post_create_is_an_empty_draft(monkeypatch):
    sent = {}

    def post_json(url, body, **kwargs):
        sent.update(url=url, body=body, kwargs=kwargs)
        return {
            "result": {
                "data": {
                    "json": {"id": 7, "publishedAt": None}
                }
            }
        }

    monkeypatch.setattr(trpc.client, "post_json", post_json)
    result = trpc.post_create(title="Scheduled")

    assert result["id"] == 7
    assert sent["url"].endswith("/post.create")
    assert sent["body"]["json"] == {"title": "Scheduled"}
    assert "meta" not in sent["body"]
    assert "images" not in sent["body"]["json"]


def test_post_add_image_cannot_send_a_publish_time(monkeypatch):
    sent = {}

    def post_json(url, body, **kwargs):
        sent.update(url=url, body=body)
        return {"result": {"data": {"json": {"id": 9, "url": body["json"]["url"]}}}}

    monkeypatch.setattr(trpc.client, "post_json", post_json)
    trpc.post_add_image(
        7,
        {
            "url": "00000000-0000-0000-0000-000000000001",
            "index": 0,
            "publishedAt": "2026-08-26T12:00:00.000Z",
        },
    )

    assert sent["url"].endswith("/post.addImage")
    assert sent["body"]["json"]["postId"] == 7
    assert "publishedAt" not in sent["body"]["json"]


def test_post_update_returns_the_publish_time_it_read_back(monkeypatch):
    expected = "2026-08-26T12:00:00.000Z"
    monkeypatch.setattr(
        trpc.client,
        "post_json",
        lambda *args, **kwargs: {"result": {"data": {"json": None}}},
    )
    monkeypatch.setattr(
        trpc.client,
        "get_json",
        lambda *args, **kwargs: {
            "result": {
                "data": {"json": {"id": 7, "publishedAt": expected}}
            }
        },
    )

    assert trpc.post_update(7, published_at=expected)["publishedAt"] == expected


def test_post_update_refuses_a_different_readback(monkeypatch):
    monkeypatch.setattr(
        trpc.client,
        "post_json",
        lambda *args, **kwargs: {"result": {"data": {"json": None}}},
    )
    monkeypatch.setattr(
        trpc.client,
        "get_json",
        lambda *args, **kwargs: {
            "result": {
                "data": {"json": {"id": 7, "publishedAt": "2026-08-26T13:00:00.000Z"}}
            }
        },
    )

    with pytest.raises(CivitaiError, match="did not keep"):
        trpc.post_update(7, published_at="2026-08-26T12:00:00.000Z")
