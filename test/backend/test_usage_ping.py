"""The optional installation ping contains exactly what its consent names."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock

from backend import config, db, usage_ping


def test_opt_in_creates_one_stable_uuid_and_opt_out_keeps_it(monkeypatch):
    monkeypatch.setattr(usage_ping.uuid, "uuid4", Mock(side_effect=["first-id", "second-id"]))

    usage_ping.set_enabled(True)
    usage_ping.set_enabled(False)
    usage_ping.set_enabled(True)

    assert usage_ping.enabled()
    assert db.get_setting(usage_ping.INSTALLATION_ID_SETTING) == "first-id"
    usage_ping.uuid.uuid4.assert_called_once_with()


def test_the_ping_sends_the_id_and_no_second_field(monkeypatch):
    post = Mock()
    monkeypatch.setenv("LIVEBOUND_USAGE_PING_URL", "https://unused.invalid/collect")
    monkeypatch.setattr(usage_ping.httpx, "post", post)

    usage_ping._send("installation-id")

    post.assert_called_once_with(
        "https://unused.invalid/collect",
        json={"id": "installation-id"},
        timeout=3.0,
    )
    assert config.usage_ping_url() == "https://unused.invalid/collect"


def test_the_ping_default_goes_to_the_public_receiver(monkeypatch):
    monkeypatch.delenv("LIVEBOUND_USAGE_PING_URL", raising=False)

    assert config.usage_ping_url() == "https://livebound.legandor.com/ping"


def test_an_opted_in_installation_starts_one_background_ping(monkeypatch):
    usage_ping.set_enabled(True)
    worker = Mock()
    thread = Mock(return_value=worker)
    monkeypatch.setattr(usage_ping, "threading", SimpleNamespace(Thread=thread))

    usage_ping.send_at_start()

    installation_id = db.get_setting(usage_ping.INSTALLATION_ID_SETTING)
    thread.assert_called_once_with(
        target=usage_ping._send,
        args=(installation_id,),
        name="livebound-usage-ping",
        daemon=True,
    )
    worker.start.assert_called_once_with()
