"""Connecting an account through the shipped OAuth client.

The failures worth catching here are the quiet ones: a scope sent in the wrong
shape (rejected at once, with an error that names nothing), a rotated refresh
token dropped on the floor (fails an hour later, far from the cause), and a
forged callback accepted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from backend import config, db
from backend.civitai import client as civitai_client
from backend.civitai import oauth
from backend.civitai.errors import CivitaiError, OAuthRegistrationRejected

REDIRECT = "http://testserver/api/oauth/callback"


@pytest.fixture(autouse=True)
def _clean_flow():
    from backend.api import oauth as oauth_api

    with oauth_api._pending.lock:
        oauth_api._pending.clear()
        oauth_api._pending.outcome = "idle"
    yield
    with oauth_api._pending.lock:
        oauth_api._pending.clear()
        oauth_api._pending.outcome = "idle"


def _install(monkeypatch, handler):
    """Put a stand-in transport into the shared pool for one test."""
    monkeypatch.setattr(
        civitai_client, "_pool", httpx.Client(transport=httpx.MockTransport(handler))
    )


def _connect(*, access="access-1", refresh="refresh-1", expires_in=3600, scope=229):
    when = datetime.now(timezone.utc) + timedelta(seconds=expires_in)
    db.set_setting(oauth.ACCESS_TOKEN_KEY, access)
    db.set_setting(oauth.REFRESH_TOKEN_KEY, refresh)
    db.set_setting(oauth.EXPIRES_AT_KEY, when.isoformat(timespec="seconds"))
    db.set_setting(oauth.SCOPE_KEY, str(scope))


def _form(request: httpx.Request) -> dict[str, str]:
    return {
        key: value[0] for key, value in parse_qs(request.content.decode()).items()
    }


# --- the application client --------------------------------------------------


def test_the_client_id_is_the_shipped_one_and_nothing_can_change_it():
    """The identity the application authenticates as is not configurable.

    It was, from the settings screen, until 2026-09-09. A row left over from
    then must not resurrect the behaviour.
    """
    db.set_setting(oauth.LEGACY_CLIENT_ID_KEY, "somebody-elses-client")
    assert oauth.client_id() == config.CIVITAI_OAUTH_CLIENT_ID


# --- the authorization URL ---------------------------------------------------


def test_the_authorize_url_carries_pkce_and_a_decimal_scope():
    """CivitAI takes an integer scope and demands S256. A standard client
    library sends 'media:write' and gets invalid_scope with nothing to go on."""
    begun = oauth.begin(REDIRECT)
    params = {k: v[0] for k, v in parse_qs(urlsplit(begun["authorize_url"]).query).items()}

    assert params["client_id"] == config.CIVITAI_OAUTH_CLIENT_ID
    assert params["scope"] == "229"
    assert params["response_type"] == "code"
    assert params["code_challenge_method"] == "S256"
    assert params["redirect_uri"] == REDIRECT
    assert params["state"] == begun["state"]
    # The challenge is the hash, never the verifier itself.
    assert begun["verifier"] not in begun["authorize_url"]


# --- exchanging the code -----------------------------------------------------


def test_the_code_exchange_sends_the_verifier(monkeypatch):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(_form(request))
        return httpx.Response(
            200,
            json={
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
                "scope": "229",
            },
        )

    _install(monkeypatch, handler)
    assert oauth.complete("code-1", "verifier-1", REDIRECT) == 229
    assert seen["grant_type"] == "authorization_code"
    assert seen["code_verifier"] == "verifier-1"
    assert seen["client_id"] == config.CIVITAI_OAUTH_CLIENT_ID
    assert oauth.connected()


def test_a_refused_exchange_says_what_to_do(monkeypatch):
    _install(monkeypatch, lambda request: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(Exception) as caught:
        oauth.complete("code-1", "verifier-1", REDIRECT)
    assert "start the connection again" in str(caught.value)
    assert not oauth.connected()


@pytest.mark.parametrize("error", ["invalid_client", "invalid_scope"])
def test_registration_rejections_name_an_available_recovery(monkeypatch, error):
    _install(monkeypatch, lambda request: httpx.Response(400, json={"error": error}))

    with pytest.raises(OAuthRegistrationRejected) as caught:
        oauth.complete("code-1", "verifier-1", REDIRECT)

    assert "Update Livebound or report the connection problem." in str(caught.value)
    assert caught.value.code == "oauth_registration_rejected"
    assert not oauth.connected()


# --- refreshing --------------------------------------------------------------


def test_a_token_outside_the_margin_is_used_as_is(monkeypatch):
    def handler(request):  # pragma: no cover - reaching it is the failure
        raise AssertionError("refreshed a token that was still good")

    _install(monkeypatch, handler)
    _connect(expires_in=3600)
    assert oauth.access_token() == "access-1"


def test_a_token_inside_the_margin_is_refreshed(monkeypatch):
    _install(
        monkeypatch,
        lambda request: httpx.Response(
            200, json={"access_token": "access-2", "refresh_token": "refresh-2", "expires_in": 3600}
        ),
    )
    _connect(expires_in=30)
    assert oauth.access_token() == "access-2"


def test_the_rotated_refresh_token_replaces_the_old_one(monkeypatch):
    """The provider hands back a new refresh token every time. Keeping the old
    one works once and then fails on the next refresh, an hour later."""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(_form(request)["refresh_token"])
        return httpx.Response(
            200,
            json={
                "access_token": f"access-{len(sent) + 1}",
                "refresh_token": f"refresh-{len(sent) + 1}",
                "expires_in": 0,
            },
        )

    _install(monkeypatch, handler)
    _connect(expires_in=0)

    oauth.access_token()
    oauth.access_token()

    assert sent == ["refresh-1", "refresh-2"]
    assert db.get_setting(oauth.REFRESH_TOKEN_KEY) == "refresh-3"


def test_a_token_response_rolls_back_if_one_setting_write_fails(monkeypatch):
    _connect(expires_in=0)
    set_setting = db.set_setting
    writes = 0

    def fail_during_second_write(key, value):
        nonlocal writes
        writes += 1
        if writes == 2:
            raise RuntimeError("injected setting write failure")
        set_setting(key, value)

    monkeypatch.setattr(db, "set_setting", fail_during_second_write)

    with pytest.raises(RuntimeError, match="injected setting write failure"):
        oauth._store(
            {
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "expires_in": 3600,
            }
        )

    assert db.get_setting(oauth.ACCESS_TOKEN_KEY) == "access-1"
    assert db.get_setting(oauth.REFRESH_TOKEN_KEY) == "refresh-1"


def test_a_refused_refresh_clears_the_connection(monkeypatch):
    """Revoked on CivitAI, or the refresh token aged out. Every later call must
    see the honest disconnected state."""
    _install(monkeypatch, lambda request: httpx.Response(400, json={"error": "invalid_grant"}))
    _connect(expires_in=0)

    assert civitai_client.get_token() is None
    assert not oauth.connected()
    assert db.get_setting(oauth.ACCESS_TOKEN_KEY) is None


def test_disconnecting_keeps_the_client_id():
    """The client is the application's identity, not the user's session."""
    _connect()
    oauth.disconnect()
    assert not oauth.connected()
    assert oauth.client_id() == config.CIVITAI_OAUTH_CLIENT_ID


def test_disconnecting_clears_the_resolved_account():
    _connect()
    db.set_json_setting("account_json", {"username": "old-account"})
    db.set_setting("civitai_username", "old-account")

    oauth.disconnect()

    assert db.get_json_setting("account_json") is None
    assert db.get_setting("civitai_username") is None


# --- the credential ----------------------------------------------------------


def test_an_old_pasted_key_is_ignored():
    db.set_setting("civitai_api_token", "pasted-key")
    assert civitai_client.get_token() is None


def test_no_credential_at_all_says_what_to_do():
    with pytest.raises(Exception) as caught:
        civitai_client.require_token()
    assert "Connect an account" in str(caught.value)


def test_half_a_connection_is_no_connection():
    """An access token without its refresh token cannot be renewed."""
    db.set_setting(oauth.ACCESS_TOKEN_KEY, "access-1")
    assert not oauth.connected()
    assert civitai_client.get_token() is None


# --- the HTTP surface --------------------------------------------------------


def test_start_hands_back_a_url_but_never_the_verifier(client):
    """The state belongs in the URL - it is the CSRF guard and has to reach
    CivitAI through the browser. The PKCE verifier is the secret, and it must
    stay here: hand it out and the code exchange is no longer bound to us."""
    from backend.api import oauth as oauth_api

    response = client.post("/api/oauth/start")
    body = response.json()

    assert body["state"] == "pending"
    assert "code_challenge=" in body["authorize_url"]
    assert oauth_api._pending.verifier
    assert oauth_api._pending.verifier not in response.text


def test_the_self_registration_route_is_gone(client):
    response = client.post("/api/oauth/register")
    assert response.status_code == 405


def test_a_callback_with_the_wrong_state_is_rejected(client, monkeypatch):
    """Without this check any page could send the browser to our callback with a
    code of its choosing."""

    def handler(request):  # pragma: no cover - reaching it is the failure
        raise AssertionError("exchanged a code from an unverified callback")

    _install(monkeypatch, handler)
    client.post("/api/oauth/start")

    response = client.get(
        "/api/oauth/callback", params={"code": "c", "state": "forged"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert not oauth.connected()
    assert client.get("/api/oauth/status").json()["state"] == "pending"


def test_a_callback_the_user_denied_reports_denied(client):
    begun = client.post("/api/oauth/start").json()
    state = parse_qs(urlsplit(begun["authorize_url"]).query)["state"][0]
    client.get(
        "/api/oauth/callback",
        params={"error": "access_denied", "state": state},
        follow_redirects=False,
    )
    assert client.get("/api/oauth/status").json()["state"] == "denied"


def test_a_successful_callback_returns_to_the_unfinished_wizard(client, monkeypatch):
    db.set_setting("setup_completed", "0")
    _install(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            json={
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
                "scope": "229",
            },
        ),
    )
    begun = client.post("/api/oauth/start").json()
    state = parse_qs(urlsplit(begun["authorize_url"]).query)["state"][0]

    response = client.get(
        "/api/oauth/callback",
        params={"code": "code-1", "state": state},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert oauth.connected()


def test_a_failed_callback_returns_to_settings_after_setup(client):
    db.set_setting("setup_completed", "1")
    begun = client.post("/api/oauth/start").json()
    state = parse_qs(urlsplit(begun["authorize_url"]).query)["state"][0]

    response = client.get(
        "/api/oauth/callback",
        params={"error": "access_denied", "state": state},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?settings=civitai"
    assert client.get("/api/oauth/status").json()["state"] == "denied"


@pytest.mark.parametrize("error", ["invalid_client", "invalid_scope", "access_denied"])
@pytest.mark.parametrize("returned_state", [None, "forged"])
def test_an_unrelated_error_callback_preserves_the_pending_connection(
    client, monkeypatch, error, returned_state
):
    from backend.api import oauth as oauth_api

    def handler(request):  # pragma: no cover - no request should be attempted
        raise AssertionError("contacted CivitAI for an unauthenticated error callback")

    _install(monkeypatch, handler)
    client.post("/api/oauth/start")
    expected, verifier = oauth_api._pending.state, oauth_api._pending.verifier
    params = {"error": error}
    if returned_state is not None:
        params["state"] = returned_state
    response = client.get("/api/oauth/callback", params=params, follow_redirects=False)
    assert response.status_code == 303
    result = client.get("/api/oauth/status").json()
    assert result["state"] == "pending"
    assert result["error_code"] is None
    assert oauth_api._pending.state == expected
    assert oauth_api._pending.verifier == verifier
    assert not oauth.connected()


@pytest.mark.parametrize("error", ["invalid_client", "invalid_scope"])
@pytest.mark.parametrize("at_authorization", [True, False])
def test_registration_recovery_reaches_the_settings_screen(
    client, monkeypatch, error, at_authorization
):
    _install(monkeypatch, lambda request: httpx.Response(400, json={"error": error}))
    begun = client.post("/api/oauth/start").json()
    state = parse_qs(urlsplit(begun["authorize_url"]).query)["state"][0]
    params = {"state": state}
    params.update({"error": error} if at_authorization else {"code": "code-1"})
    response = client.get("/api/oauth/callback", params=params, follow_redirects=False)
    assert response.status_code == 303
    result = client.get("/api/oauth/status").json()
    assert result["state"] == "failed"
    assert result["error_code"] == "oauth_registration_rejected"
    assert not oauth.connected()

    # A fresh attempt must not display a previous attempt's recovery message.
    client.post("/api/oauth/start")
    assert client.get("/api/oauth/status").json()["error_code"] is None


def test_status_reports_the_connection_without_the_token(client):
    _connect()
    body = client.get("/api/oauth/status").json()
    assert body["state"] == "connected"
    assert body["scope"] == 229
    assert "registered" not in body
    assert "blocker" not in body


def test_the_settings_response_names_the_connection_state(client):
    assert client.get("/api/settings").json()["civitai_auth_mode"] == "none"
    db.set_setting("civitai_api_token", "pasted-key")
    assert client.get("/api/settings").json()["civitai_auth_mode"] == "none"
    _connect()
    assert client.get("/api/settings").json()["civitai_auth_mode"] == "oauth"


def test_the_account_endpoint_resolves_a_fresh_connection(client, monkeypatch):
    """The callback returns to a shell that immediately reads this endpoint."""
    from backend.civitai import account

    _connect()
    calls = 0

    def resolve():
        nonlocal calls
        calls += 1
        value = {"username": "fresh-account", "tokenScope": 229}
        db.set_json_setting("account_json", value)
        db.set_setting("civitai_username", "fresh-account")
        return value

    monkeypatch.setattr(account, "refresh", resolve)
    body = client.get("/api/account").json()
    assert calls == 1
    assert body["username"] == "fresh-account"
    assert body["auth_mode"] == "oauth"


def test_account_resolution_failure_does_not_fail_the_connection(client, monkeypatch):
    from backend.civitai import account

    _connect()

    def refuse():
        raise CivitaiError("temporary profile lookup failure")

    monkeypatch.setattr(account, "refresh", refuse)
    response = client.get("/api/account")

    assert response.status_code == 200
    assert response.json()["account"] is None
    assert oauth.connected()


def test_the_client_id_route_is_gone(client):
    """It let the application be pointed at somebody else's registration.

    Removed on 2026-09-09: a fork that wants different permissions registers its
    own client in `config.py`, where the change is visible in the source rather
    than talked into a running installation.
    """
    response = client.post("/api/oauth/client-id", json={"client_id": "abc-123"})
    # 405 rather than 404 because the path is still matched by another method;
    # either way there is no route that accepts this.
    assert response.status_code in (404, 405)
    assert oauth.client_id() == config.CIVITAI_OAUTH_CLIENT_ID


def test_the_granted_scope_survives_either_response_shape(monkeypatch):
    """The device-token endpoint answers `scope` as a string, the token endpoint
    as a one-element list. str() of the second stores "['229']", which parses as
    nothing, and the screen then says the connection grants no permissions."""
    for shape in ("229", ["229"], 229, [229]):
        oauth.disconnect()
        _install(
            monkeypatch,
            lambda request, shape=shape: httpx.Response(
                200,
                json={
                    "access_token": "a",
                    "refresh_token": "r",
                    "expires_in": 3600,
                    "scope": shape,
                },
            ),
        )
        assert oauth.complete("code", "verifier", REDIRECT) == 229, shape
        assert oauth.scope() == 229, shape
