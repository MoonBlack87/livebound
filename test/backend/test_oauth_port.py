"""Regression coverage for OAuth callbacks on non-default loopback ports.

This proves Livebound carries the actual local port through both halves of the
PKCE exchange. CivitAI's acceptance of that redirect is an upstream contract and
must be verified separately against its source or the live provider.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from backend import config
from backend.civitai import client as civitai_client
from backend.civitai import oauth


@pytest.fixture(autouse=True)
def _clean_pending_flow():
    from backend.api import oauth as oauth_api

    with oauth_api._pending.lock:
        oauth_api._pending.clear()
        oauth_api._pending.outcome = "idle"
    yield
    with oauth_api._pending.lock:
        oauth_api._pending.clear()
        oauth_api._pending.outcome = "idle"


def test_nondefault_loopback_port_survives_authorize_and_exchange(client, monkeypatch):
    sent: dict[str, str] = {}

    def token_exchange(request: httpx.Request) -> httpx.Response:
        sent.update(
            {
                key: value[0]
                for key, value in parse_qs(request.content.decode()).items()
            }
        )
        return httpx.Response(
            200,
            json={
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
                "scope": "229",
            },
        )

    monkeypatch.setattr(
        civitai_client,
        "_pool",
        httpx.Client(transport=httpx.MockTransport(token_exchange)),
    )

    host = "127.0.0.1:9000"
    expected_redirect = f"http://{host}{config.OAUTH_REDIRECT_PATH}"

    begun = client.post("/api/oauth/start", headers={"host": host}).json()
    authorize_params = {
        key: value[0]
        for key, value in parse_qs(urlsplit(begun["authorize_url"]).query).items()
    }

    assert authorize_params["redirect_uri"] == expected_redirect

    response = client.get(
        "/api/oauth/callback",
        params={"code": "code-1", "state": authorize_params["state"]},
        headers={"host": host},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert sent["redirect_uri"] == expected_redirect
    assert oauth.connected()
