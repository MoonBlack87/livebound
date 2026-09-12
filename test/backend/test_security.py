"""The guards around a local app that holds a credential."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath

import httpx
import pytest
from fastapi import HTTPException

from backend import db, security


@pytest.mark.parametrize(
    ("path_type", "root", "path", "expected"),
    [
        (PurePosixPath, "/images", "/IMAGES/example.png", False),
        (PureWindowsPath, "C:/images", "c:/IMAGES/example.png", True),
    ],
)
def test_containment_uses_the_path_flavour_case_rules(path_type, root, path, expected):
    assert security.is_under_any_root(path_type(path), [path_type(root)]) is expected


@pytest.mark.parametrize(
    ("path_type", "root", "sibling"),
    [
        (PurePosixPath, "/images", "/images2/example.png"),
        (PureWindowsPath, "C:/images", "C:/images2/example.png"),
    ],
)
def test_containment_does_not_confuse_a_prefixed_sibling_for_a_child(path_type, root, sibling):
    assert not security.is_under_any_root(path_type(sibling), [path_type(root)])


def test_containment_does_not_cross_windows_drives():
    assert not security.is_under_any_root(
        PureWindowsPath("D:/images/example.png"), [PureWindowsPath("C:/images")]
    )


def test_a_foreign_host_header_is_rejected(client):
    """DNS rebinding: the browser sends the attacker's hostname here, and it
    will never look like loopback."""
    response = client.get("/api/health", headers={"Host": "angreifer.example.com"})
    assert response.status_code == 421


def test_loopback_hosts_pass(client):
    for host in ("localhost", "127.0.0.1", "localhost:8430", "127.0.0.1:8430"):
        assert client.get("/api/health", headers={"Host": host}).status_code == 200


def test_an_explicitly_allowed_host_passes(client, monkeypatch):
    """Reaching the app from another machine has to be a deliberate act."""
    monkeypatch.setenv("LIVEBOUND_ALLOWED_HOSTS", "meinpc.fritz.box")
    assert client.get("/api/health", headers={"Host": "meinpc.fritz.box"}).status_code == 200
    assert client.get("/api/health", headers={"Host": "anderer.host"}).status_code == 421


def test_a_file_outside_the_configured_folders_is_refused(client, scanned, tmp_path):
    outside = tmp_path / "geheim.txt"
    outside.write_text("not for the browser")
    with pytest.raises(HTTPException) as info:
        security.serveable(outside)
    assert info.value.status_code == 403


def test_a_file_inside_a_configured_folder_is_served(client, scanned):
    from backend.store import images as image_store

    image = image_store.get(scanned[0])
    assert security.serveable(Path(image["absolute_path"])).exists()


def test_a_symlink_out_of_the_folder_does_not_escape(client, scanned, tmp_path, fixture_images):
    """Resolving first is what makes this hold - a link is not a way out."""
    folder, _ = fixture_images
    secret = tmp_path / "outside.txt"
    secret.write_text("geheim")
    link = folder / "escape.png"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlinks not possible")

    with pytest.raises(HTTPException) as info:
        security.serveable(link)
    assert info.value.status_code == 403


def test_a_missing_file_is_a_404_not_a_leak(tmp_path):
    with pytest.raises(HTTPException) as info:
        security.serveable(tmp_path / "gibtsnicht.png")
    assert info.value.status_code == 404


def test_binding_to_a_lan_address_warns():
    assert security.warn_if_exposed("127.0.0.1") is None
    assert security.warn_if_exposed("192.168.1.50") is not None
    assert security.warn_if_exposed("0.0.0.0") is not None


def test_the_api_call_log_never_stores_the_credential():
    from backend.civitai.client import _scrub

    cleaned = _scrub(
        {"Authorization": "Bearer geheim", "token": "geheim", "data": "A" * 4000, "ok": 1}
    )
    assert cleaned["Authorization"] == "***"
    assert cleaned["token"] == "***"
    assert cleaned["data"] == {"type": "str", "length": 4000}
    assert cleaned["ok"] == 1


def test_diagnostics_start_off(client):
    from backend import config

    assert client.get("/api/settings").json()["debug_logging"] is False
    assert db.get_setting("debug_logging") == "0"
    assert client.post("/api/posts", json={"model_version_id": "not-an-id"}).status_code == 422
    assert config.diagnostic_log_path().read_text(encoding="utf-8") == ""


def test_the_rotating_diagnostic_log_is_bounded_and_omits_request_data(client):
    from logging.handlers import RotatingFileHandler

    from backend import config, diagnostics
    from backend.civitai import oauth

    token = "synthetic-api-token"
    authorization = "synthetic-authorization"
    cookie = "synthetic-cookie"
    prompt = "synthetic-prompt-text"
    metadata = "synthetic-image-metadata"
    query_value = "synthetic-query-value"
    body_value = "synthetic-body-value"

    db.set_setting("debug_logging", "1")
    db.set_setting(oauth.ACCESS_TOKEN_KEY, token)
    response = client.post(
        "/api/posts",
        headers={"Authorization": authorization, "Cookie": cookie},
        json={
            "model_version_id": body_value,
            "detail": prompt,
            "metadata": metadata,
        },
    )
    query_response = client.get(
        "/api/images",
        params={"limit": query_value},
        headers={"Authorization": authorization, "Cookie": cookie},
    )
    assert response.status_code == query_response.status_code == 422

    retained = "\n".join(
        path.read_text(encoding="utf-8")
        for path in config.data_dir().glob(f"{config.DIAGNOSTIC_LOG_FILENAME}*")
    )
    for secret in (token, authorization, cookie, prompt, metadata, query_value, body_value):
        assert secret not in retained

    handler = next(
        handler
        for handler in diagnostics._logger.handlers
        if isinstance(handler, RotatingFileHandler)
    )
    assert Path(handler.baseFilename) == config.diagnostic_log_path().resolve()
    assert handler.maxBytes == config.DIAGNOSTIC_LOG_MAX_BYTES
    assert handler.backupCount == config.DIAGNOSTIC_LOG_BACKUP_COUNT
    assert handler.maxBytes * (handler.backupCount + 1) == 3 * 1024**2


def test_scrubbing_reaches_into_nested_payloads():
    from backend.civitai.client import _scrub

    cleaned = _scrub({"params": {"arguments": {"data": "B" * 500, "api_key": "x"}}})
    assert cleaned["params"]["arguments"]["api_key"] == "***"
    assert cleaned["params"]["arguments"]["data"] == {"type": "str", "length": 500}


def test_scrubbing_removes_credentials_from_presigned_urls():
    from backend.civitai.client import _scrub

    signed = (
        "https://access:secret@uploads.example/image.png?part=1"
        "&X-Amz-Credential=account%2Fscope&X-Amz-Signature=signed-value#private"
    )

    cleaned = _scrub({"result": {"data": {"uploadURL": signed}}})

    assert cleaned["result"]["data"]["uploadURL"] == (
        "https://uploads.example/image.png"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://uploads.example/image?signature=private",
        "ftp://uploads.example/image?signature=private",
        "file:///tmp/image?signature=private",
        "//uploads.example/image?signature=private",
        "relative/image?signature=private",
        "http://[invalid/image?signature=private",
    ],
)
def test_presigned_upload_rejects_non_https_before_reading_or_sending(
    monkeypatch, tmp_path, url
):
    from backend.api.common import handle_civitai
    from backend.civitai import client as http
    from backend.civitai import rest
    from backend.civitai.errors import UploadUrlRejected

    monkeypatch.setattr(
        http, "post_json", lambda *args, **kwargs: {"id": "upload-1", "uploadURL": url}
    )

    def no_transport():  # pragma: no cover - rejection must precede transport
        raise AssertionError("attempted an upload to a non-HTTPS URL")

    monkeypatch.setattr(http, "http_client", no_transport)
    # A file open would fail too, proving rejection precedes reading its bytes.
    with pytest.raises(UploadUrlRejected) as caught:
        rest.upload_presigned(tmp_path / "not-created.png")
    assert "private" not in str(caught.value)
    assert handle_civitai(caught.value).detail["code"] == "upload_url_requires_https"


def test_presigned_https_upload_keeps_the_bytes_headers_and_identity(monkeypatch, tmp_path):
    from backend.civitai import client as http
    from backend.civitai import rest

    path = tmp_path / "image.png"
    original = b"source image bytes"
    path.write_bytes(original)
    url = "https://uploads.example/image?signature=signed-value"
    monkeypatch.setattr(
        http, "post_json", lambda *args, **kwargs: {"id": "upload-1", "uploadURL": url}
    )
    requests = []

    def upload(request):
        requests.append(request)
        assert request.method == "PUT"
        assert str(request.url) == url
        assert request.content == original
        assert request.headers["Content-Type"] == "image/png"
        assert "Authorization" not in request.headers
        return httpx.Response(200)

    with httpx.Client(transport=httpx.MockTransport(upload)) as transport:
        monkeypatch.setattr(http, "http_client", lambda: transport)
        result = rest.upload_presigned(path, content_type="image/png")
    assert len(requests) == 1
    assert result == {"uuid": "upload-1", "content_type": "image/png"}
    assert path.read_bytes() == original


def test_api_call_log_keeps_structure_without_post_content():
    from backend.civitai.client import log_call

    secrets = {
        "title": "private-api-log-title",
        "description": "private-api-log-description",
        "prompt": "private-api-log-prompt",
        "meta": "private-api-log-metadata",
    }
    raw_response = json.dumps({"id": 18, **secrets})
    log_call(
        "trpc",
        "post.update",
        status=500,
        duration_ms=37,
        ok=False,
        request={"json": {"id": 17, **secrets}},
        response=raw_response,
    )
    assert db.get_connection().execute("SELECT COUNT(*) FROM api_calls").fetchone()[0] == 0

    db.set_setting("debug_logging", "1")
    log_call(
        "trpc",
        "post.update",
        status=500,
        duration_ms=37,
        ok=False,
        request={"json": {"id": 17, **secrets}},
        response=raw_response,
    )

    row = db.get_connection().execute(
        "SELECT * FROM api_calls WHERE procedure='post.update' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["http_status"] == 500
    assert row["duration_ms"] == 37
    assert row["ok"] == 0
    assert json.loads(row["request_json"]) == {"json": {"id": 17}}
    assert json.loads(row["response_json"]) == {
        "error_type": "CivitaiError",
        "body": {"type": "str", "length": len(raw_response)},
    }
    retained = f"{row['request_json']}\n{row['response_json']}"
    assert all(value not in retained for value in secrets.values())


def test_unbounded_collections_are_refused(client):
    """A runaway script or a typo must not hand the database a million rows to
    chew through while holding a write lock."""
    response = client.post("/api/posts", json={"image_ids": list(range(5000))})
    assert response.status_code == 422


def test_an_invalid_publish_mode_is_refused_not_silently_reinterpreted(client):
    """"banana" used to fall through to "schedule". Typed states cannot."""
    response = client.post("/api/posts", json={"publish_mode": "banana"})
    assert response.status_code == 422


def test_an_invalid_schedule_mode_is_refused(client):
    assert client.post("/api/posts", json={"schedule_mode": "irgendwas"}).status_code == 422


def test_absurd_offsets_are_refused(client):
    assert client.post("/api/posts", json={"schedule_offset_minutes": -5}).status_code == 422
    assert (
        client.post("/api/posts", json={"schedule_offset_minutes": 99_999_999}).status_code == 422
    )


def test_an_overlong_title_is_refused(client):
    assert client.post("/api/posts", json={"title": "x" * 5000}).status_code == 422


def test_a_zero_interval_spread_is_refused(client):
    response = client.post(
        "/api/schedule/spread",
        json={"post_ids": [1], "start_offset_minutes": 90, "interval_minutes": 0},
    )
    assert response.status_code == 422


def test_a_profile_edit_cannot_put_a_stray_value_in_a_numeric_column(client):
    """PATCH used to take a raw dict while POST was bounded, so a value the
    create path rejects could still be written by editing. It stays partial -
    demanding a full body would break every single-field edit."""
    created = client.post(
        "/api/llm/profiles",
        json={"name": "Bounds", "system_prompt": "Answer in JSON."},
    ).json()

    refused = client.patch(f"/api/llm/profiles/{created['id']}", json={"temperature": "hot"})
    assert refused.status_code == 422

    out_of_range = client.patch(f"/api/llm/profiles/{created['id']}", json={"temperature": 9})
    assert out_of_range.status_code == 422

    partial = client.patch(f"/api/llm/profiles/{created['id']}", json={"tag_count": 3})
    assert partial.status_code == 200
    assert (partial.json()["tag_count"], partial.json()["name"]) == (3, "Bounds")


def test_a_write_from_a_foreign_page_is_refused(client):
    """The host check does not cover this. A page anywhere can POST to an
    absolute http://127.0.0.1:8430/... URL - the Host is loopback, so it sails
    through - and the effects here include deleting a post that CivitAI cannot
    undelete."""
    response = client.put(
        "/api/settings", json={}, headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "foreign_origin"


def test_the_cross_site_signal_is_enough_on_its_own(client):
    """Some browsers omit Origin where Sec-Fetch-Site still says cross-site."""
    response = client.put(
        "/api/settings", json={}, headers={"Sec-Fetch-Site": "cross-site"}
    )

    assert response.status_code == 403


def test_the_app_s_own_requests_pass(client):
    response = client.put(
        "/api/settings",
        json={},
        headers={"Origin": "http://127.0.0.1:8430", "Sec-Fetch-Site": "same-origin"},
    )

    assert response.status_code == 200


def test_a_tool_that_sends_no_origin_passes(client):
    """curl, the smoke test, this test client. Those are the user's own tools;
    the guard is about a page the user did not write."""
    assert client.put("/api/settings", json={}).status_code == 200


def test_reading_is_never_refused_by_origin(client):
    """A cross-origin GET cannot be read back, so there is nothing to guard."""
    response = client.get("/api/health", headers={"Origin": "https://evil.example"})

    assert response.status_code == 200


def test_the_api_key_is_only_sent_to_civitai(client):
    """Every REST and tRPC call carries the token. Where that goes is not a free
    choice: a pasted value would hand a credential with delete access to a
    stranger, and http:// would hand it to anyone on the path."""
    from backend import config

    assert config.site_host_allowed("https://civitai.red")
    assert config.site_host_allowed("https://api.civitai.com")
    assert not config.site_host_allowed("http://civitai.com"), "not over plain http"
    assert not config.site_host_allowed("https://evil.example")
    assert not config.site_host_allowed("https://civitai.red.evil.example"), (
        "a suffix is not a subdomain"
    )
    assert not config.site_host_allowed("https://civitai.red:443@evil.example"), (
        "URL userinfo must not disguise the actual destination host"
    )
    assert not config.site_host_allowed("https://user@evil.example")
    assert not config.site_host_allowed("https://user@civitai.red"), (
        "userinfo is rejected even when the destination host itself is allowed"
    )

    refused = client.put("/api/settings", json={"site_base": "https://evil.example"})
    assert refused.status_code == 400
    assert refused.json()["detail"]["code"] == "bad_site"
    assert config.site_base() != "https://evil.example"


def test_unauthenticated_calls_never_send_the_stored_token(monkeypatch):
    from backend import db
    from backend.civitai import client as civitai_client

    db.set_setting("civitai_api_token", "synthetic-api-token")
    sent_headers = []

    class RecordingClient:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def close(self):
            pass

        def post(self, url, *, json, headers):
            sent_headers.append(headers)
            return httpx.Response(200, json={})

        def get(self, url, *, params, headers):
            sent_headers.append(headers)
            return httpx.Response(200, json={})

    monkeypatch.setattr(civitai_client.httpx, "Client", lambda **kwargs: RecordingClient())
    civitai_client.close_http_client()

    civitai_client.post_json(
        "https://public.example/api",
        {},
        procedure="public.post",
        transport="rest",
        authenticated=False,
    )
    civitai_client.get_json(
        "https://public.example/api",
        procedure="public.get",
        transport="rest",
        authenticated=False,
    )

    assert len(sent_headers) == 2
    assert all("Authorization" not in headers for headers in sent_headers)


def test_an_authenticated_call_rechecks_the_configured_site(monkeypatch):
    from backend import config, db
    from backend.civitai import client as civitai_client
    from backend.civitai.errors import AuthError

    db.set_setting("civitai_api_token", "synthetic-api-token")
    db.set_setting("site_base", "https://civitai.red:443@evil.example")
    monkeypatch.setattr(
        civitai_client.httpx,
        "Client",
        lambda **kwargs: pytest.fail("no request may be built for a refused site"),
    )
    civitai_client.close_http_client()

    with pytest.raises(AuthError, match="not allowed to receive"):
        civitai_client.post_json(
            config.trpc_base(),
            {},
            procedure="post.get",
            transport="trpc",
        )


def test_a_local_mock_can_be_allowed_through_the_environment(client, monkeypatch):
    """Deliberately an environment variable and not a setting, so it cannot be
    reached from the browser."""
    from backend import config

    monkeypatch.setenv("LIVEBOUND_ALLOWED_SITES", "127.0.0.1")

    assert config.site_host_allowed("http://127.0.0.1:9999")
    response = client.put("/api/settings", json={"site_base": "http://127.0.0.1:9999"})
    assert response.status_code == 200


def test_a_read_inside_a_transaction_is_protected_from_another_writer(client, scanned):
    """sqlite3's default mode emits BEGIN before the first *write*, so a SELECT
    inside the block ran outside any transaction. replace_resources() reads the
    user's override flags and then deletes the rows - a scan overlapping an edit
    read the flag as unset and threw it away."""
    import threading

    from backend import db

    started = threading.Event()
    blocked = threading.Event()

    def other_writer() -> None:
        started.wait(5)
        try:
            with db.transaction() as conn:
                conn.execute("UPDATE posts SET title='from the other thread'")
        except Exception:
            pass
        finally:
            blocked.set()
            db.close_connection()

    thread = threading.Thread(target=other_writer, daemon=True)
    thread.start()
    with db.transaction() as conn:
        assert conn.in_transaction, "the lock is held from the start, not from the first write"
        started.set()
        # The other thread cannot commit while this block holds the write lock.
        assert not blocked.wait(0.3), "a second writer waits instead of interleaving"
    thread.join(timeout=5)
