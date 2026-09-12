"""The optional, deliberately minimal installation ping.

Consent and the installation id live in the settings table. The request has
one field and its receiver is published alongside this application.
"""

from __future__ import annotations

import contextlib
import threading
import uuid

import httpx

from . import config, db

ENABLED_SETTING = "usage_ping_enabled"
INSTALLATION_ID_SETTING = "usage_ping_installation_id"


def enabled() -> bool:
    return db.get_setting(ENABLED_SETTING) == "1"


def set_enabled(value: bool) -> None:
    """Persist consent, creating the stable id only after an opt-in."""
    if value and db.get_setting(INSTALLATION_ID_SETTING) is None:
        db.set_setting(INSTALLATION_ID_SETTING, str(uuid.uuid4()))
    db.set_setting(ENABLED_SETTING, "1" if value else "0")


def send_at_start() -> None:
    """Send this start's ping in the background when the user opted in."""
    if not enabled():
        return
    installation_id = db.get_setting(INSTALLATION_ID_SETTING)
    if installation_id is None:
        # An enabled row without an id can only come from a hand-edited or old
        # database. Consent still exists, so repair it before sending.
        installation_id = str(uuid.uuid4())
        db.set_setting(INSTALLATION_ID_SETTING, installation_id)
    threading.Thread(
        target=_send,
        args=(installation_id,),
        name="livebound-usage-ping",
        daemon=True,
    ).start()


def _send(installation_id: str) -> None:
    # A receiver failure must never prevent this local application from starting.
    with contextlib.suppress(httpx.HTTPError):
        httpx.post(
            config.usage_ping_url(),
            json={"id": installation_id},
            timeout=3.0,
        )
