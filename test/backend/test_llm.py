"""Prompt building, output normalisation, and how the model is loaded at all.

The model itself is not exercised here - that needs a GPU and a few gigabytes.
What is tested is everything around it, because that is where the output becomes
a post title, and because loading a checkpoint is where a model directory could
otherwise run its own code.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import types
from contextlib import nullcontext

import pytest
from fastapi.testclient import TestClient

from backend import db, jobs
from backend.llm import endpoint, model_loader, profiles, prompts, service, worker
from backend.posts import lifecycle
from backend.store import posts as post_store


def _profile_payload(name: str = "Custom") -> dict:
    return {
        "name": name,
        "description": "A test profile.",
        "system_prompt": "Describe the image as JSON.",
        "title_max_words": 6,
        "tag_count": 5,
        "include_images": True,
        "max_images": 3,
        "vision_max_side": 640,
        "max_new_tokens": 800,
        "temperature": 0.6,
        "top_p": 0.9,
        "top_k": 30,
    }


def _insert_suggestion(post_id: int, field: str, text: str) -> int:
    with db.transaction() as conn:
        cursor = conn.execute(
            "INSERT INTO llm_suggestions(post_id, field, text, created_at) VALUES(?,?,?,?)",
            (post_id, field, text, db.now_iso()),
        )
    return int(cursor.lastrowid)


class _ScriptedWorker:
    """Answers with the next scripted JSON, so a run is deterministic."""

    def __init__(self, answers: list[str]):
        self.answers = list(answers)
        self.requests: list[dict] = []
        self.info = {"device": "cpu"}

    def generate(self, request):
        self.requests.append(request)
        return self.answers.pop(0)


class _BlockingWorker(_ScriptedWorker):
    """Returns one result, then holds the next generation open."""

    def __init__(self, answers: list[str], sleep: float):
        super().__init__(answers)
        self.sleep = sleep
        self.waiting = threading.Event()

    def generate(self, request):
        self.requests.append(request)
        if len(self.requests) == 2:
            self.waiting.set()
            time.sleep(self.sleep)
        return self.answers.pop(0)


class _FailingWorker:
    info = {"device": "cpu"}

    def generate(self, _request):
        raise RuntimeError("model broke")


def _answer(title: str, description: str, tags: list[str]) -> str:
    return json.dumps({"title": title, "description": description, "tags": tags, "reason": "r"})


def test_application_shutdown_stops_the_worker(monkeypatch):
    from backend import main

    stopped: list[None] = []
    monkeypatch.setattr(main.llm_service, "stop_worker", lambda: stopped.append(None))

    with TestClient(main.create_app()):
        pass

    assert stopped == [None]


def test_application_shutdown_without_a_worker_is_quiet(monkeypatch, caplog):
    from backend import main

    monkeypatch.setattr(service, "_worker", None)

    with TestClient(main.create_app()):
        pass

    # Quiet is the assertion, not merely "did not raise": the ordinary shutdown
    # has no worker to stop, so a warning here would appear on every exit.
    assert "Could not stop the local model worker" not in caplog.text


def test_application_shutdown_continues_when_stopping_the_worker_fails(monkeypatch, caplog):
    from backend import main

    def stop_worker() -> None:
        raise RuntimeError("worker stop failed")

    monkeypatch.setattr(main.llm_service, "stop_worker", stop_worker)

    with TestClient(main.create_app()):
        pass

    assert "Could not stop the local model worker during shutdown" in caplog.text


def test_one_run_fills_all_three_fields_and_repeats_for_more_variants(
    client, scanned, monkeypatch
):
    """A single generation already answers title, description and tags, so
    asking field by field paid for two extra model loads and discarded two
    thirds of every answer. The variant count is what costs a generation - and
    only that, because the model stays loaded across them.
    """
    post_id = client.post("/api/posts", json={"image_ids": scanned[:1]}).json()["id"]
    db.set_setting("llm_variants", "3")
    worker = _ScriptedWorker(
        [
            _answer("First title", "First description", ["alpha", "beta"]),
            _answer("Second title", "Second description", ["gamma"]),
            # The third repeats the first word for word.
            _answer("First title", "First description", ["alpha", "beta"]),
        ]
    )
    starts: list[int] = []
    stops: list[int] = []
    monkeypatch.setattr(service, "start_worker", lambda: (starts.append(1), worker)[1])
    monkeypatch.setattr(service, "stop_worker", lambda: stops.append(1))

    job = jobs.Job(id=0, kind="llm")
    service.suggest(job, [post_id], None, None)

    assert len(worker.requests) == 3, "three variants are three generations"
    assert (len(starts), len(stops)) == (1, 1), "loaded once, released once"
    assert job.total == 3

    rows = db.get_connection().execute(
        "SELECT field, text FROM llm_suggestions WHERE post_id=? ORDER BY id", (post_id,)
    ).fetchall()
    stored = [(row["field"], row["text"]) for row in rows]
    assert ("title", "First title") in stored
    assert ("description", "First description") in stored
    assert ("tags", "alpha, beta") in stored
    assert ("title", "Second title") in stored
    # The repeat is one suggestion, not two.
    assert stored.count(("title", "First title")) == 1
    assert len(stored) == 6, stored
    assert (job.succeeded, job.processed) == (2, 3)


def test_an_abandoned_generation_still_gives_the_model_back(client, scanned, monkeypatch):
    """Cancelling must not leave the model resident on a GPU shared with image generation."""
    post_id = client.post("/api/posts", json={"image_ids": scanned[:1]}).json()["id"]
    db.set_setting("llm_variants", "2")
    worker = _BlockingWorker(
        [_answer("Kept", "Already made", ["kept"]), _answer("Late", "Dropped", ["late"])],
        sleep=1.0,
    )
    released = threading.Event()
    monkeypatch.setattr(service, "start_worker", lambda: worker)
    monkeypatch.setattr(service, "stop_worker", lambda **_kwargs: released.set())

    job = jobs.start("llm", lambda running: service.suggest(running, [post_id], None))
    assert worker.waiting.wait(timeout=1), "the second generation is waiting"
    job.cancel()

    assert released.wait(timeout=2), "the worker is released after an abandoned generation"


def test_cancelling_reaches_a_waiting_generation(client, scanned, monkeypatch):
    """A cancel must not wait for an adapter that has no stop mechanism."""
    post_id = client.post("/api/posts", json={"image_ids": scanned[:1]}).json()["id"]
    db.set_setting("llm_variants", "2")
    worker = _BlockingWorker(
        [_answer("Kept", "Already made", ["kept"]), _answer("Late", "Dropped", ["late"])],
        sleep=1.0,
    )
    monkeypatch.setattr(service, "start_worker", lambda: worker)
    monkeypatch.setattr(service, "stop_worker", lambda **_kwargs: None)

    job = jobs.start("llm", lambda running: service.suggest(running, [post_id], None))
    assert worker.waiting.wait(timeout=1), "the second generation is waiting"
    started = time.monotonic()
    cancel = threading.Thread(target=job.cancel)
    cancel.start()
    cancel.join()
    while job.status in ("starting", "running") and time.monotonic() - started < 0.5:
        time.sleep(0.01)

    assert time.monotonic() - started < 0.5, "cancel returned well before generation"
    assert job.status == "cancelled"
    assert len(job.result["items"]) == 1
    assert job.result["items"][0]["title"] == "Kept"


def test_a_generation_exception_reaches_the_existing_failure_handler(
    client, monkeypatch
):
    """A thread must not turn a broken model into a silent empty run."""
    post_ids = [post_store.create(title=f"Post {number}") for number in range(3)]
    monkeypatch.setattr(service, "start_worker", _FailingWorker)
    monkeypatch.setattr(service, "stop_worker", lambda **_kwargs: None)

    job = jobs.Job(id=0, kind="llm")
    service.suggest(job, post_ids, None)

    assert job.failed == 3
    assert job.error == "model broke"
    assert [item["message"] for item in job.items] == ["model broke"] * 3


def test_a_picked_server_model_is_the_active_one(client, monkeypatch):
    """Readiness used to mean "an interpreter and a model folder exist". With a
    hosted model there is neither, and demanding them would leave the endpoint
    route permanently not ready - the suggestion routes refuse before they even
    start."""
    db.set_setting("llm_model_dir", "")
    db.set_setting("llm_python", "")

    local = service.status()
    assert (local["runtime"], local["ready"]) == ("local", False)

    db.set_setting("llm_endpoint_model", "ministral-3b")
    hosted = service.status()
    assert (hosted["runtime"], hosted["ready"]) == ("endpoint", True)
    assert hosted["endpoint"] == endpoint.DEFAULT_URL

    # No real server may be touched here. Unmocked, this reached the maintainer's
    # running Ollama on every pytest run - `Endpoint.__init__` asks it what the
    # model can do, and `stop_worker()` then told it to unload.
    monkeypatch.setattr(endpoint, "_post", lambda url, body, timeout: {"capabilities": []})
    monkeypatch.setattr(service, "_worker", None)
    started = service.start_worker()
    assert isinstance(started, endpoint.Endpoint), "no subprocess for a hosted model"
    assert started.model == "ministral-3b"
    service.stop_worker()


def test_pictures_ride_on_the_message_not_in_the_text(monkeypatch):
    """Ollama takes images as a field on the user message. The OpenAI shim on
    `/v1` renders them as content parts instead, and Mistral's chat template
    answers that with a 500 - measured, which is why this speaks `/api/chat`."""
    sent: list[tuple[str, dict]] = []

    def fake_post(url, body, timeout):
        if url.endswith("/api/show"):
            return {"capabilities": ["completion", "vision"]}
        sent.append((url, body))
        return {"message": {"content": " Answer "}}

    monkeypatch.setattr(endpoint, "_post", fake_post)
    hosted = endpoint.Endpoint(None, "ministral-3b")

    assert hosted.generate({"user_content": "Name it.", "system_prompt": "S"}) == "Answer"
    url, body = sent[0]
    assert url.endswith("/api/chat")
    assert body["messages"][1] == {"role": "user", "content": "Name it."}, "no empty images key"

    hosted.generate({"user_content": "Name it.", "images": ["QUJD", "REVG"]})
    assert sent[1][1]["messages"][1]["images"] == ["QUJD", "REVG"]


def test_a_text_only_server_model_is_known_before_the_pictures_are_sent(monkeypatch):
    """Ollama answers "image input is not supported" with a 500 in the middle of
    a batch. It also reports `capabilities` up front, so the run degrades to
    text before it starts - and says why, which is what the settings page shows.
    """
    sent: list[dict] = []

    def fake_post(url, body, timeout):
        if url.endswith("/api/show"):
            return {"capabilities": ["tools", "completion"]}
        sent.append(body)
        return {"message": {"content": "Answer"}}

    monkeypatch.setattr(endpoint, "_post", fake_post)
    hosted = endpoint.Endpoint(None, "ministral-3b")

    assert hosted.vision is False
    assert "mmproj" in hosted.vision_error
    hosted.generate({"user_content": "Name it.", "images": ["QUJD"]})
    assert "images" not in sent[0]["messages"][1], "no pictures were sent"


def test_each_variant_draws_its_own_seed_and_keeps_it(client, scanned, monkeypatch):
    """A seed nobody chose cannot be reported back - torch and llama.cpp do not
    say what they drew. So the application draws it, and each variant draws its
    own, or three alternatives would be one answer three times."""
    post_id = client.post("/api/posts", json={"image_ids": scanned[:1]}).json()["id"]
    db.set_setting("llm_variants", "3")
    answers = [_answer(f"Title {n}", f"Description {n}", [f"tag{n}"]) for n in (1, 2, 3)]
    worker = _ScriptedWorker(answers)
    monkeypatch.setattr(service, "start_worker", lambda: worker)
    monkeypatch.setattr(service, "stop_worker", lambda **_kwargs: None)

    service.suggest(jobs.Job(id=0, kind="llm"), [post_id], None, None)

    sent = [request["seed"] for request in worker.requests]
    assert all(isinstance(value, int) for value in sent), sent
    assert len(set(sent)) == 3, "three variants, three seeds"

    stored = {
        row["seed"]
        for row in db.get_connection().execute(
            "SELECT seed FROM llm_suggestions WHERE post_id=?", (post_id,)
        )
    }
    assert stored == set(sent), "what was used is what was written down"


def test_a_given_seed_is_used_unchanged(client, scanned, monkeypatch):
    """Entering the seed of a discarded suggestion has to reach the model, or
    the promise that it comes back is empty."""
    post_id = client.post("/api/posts", json={"image_ids": scanned[:1]}).json()["id"]
    # Three variants configured on purpose: a given seed must collapse them to
    # one, or the same text is generated three times and two are dropped as
    # duplicates while the user waits for all three.
    db.set_setting("llm_variants", "3")
    worker = _ScriptedWorker([_answer("Title", "Description", ["tag"])])
    monkeypatch.setattr(service, "start_worker", lambda: worker)
    monkeypatch.setattr(service, "stop_worker", lambda **_kwargs: None)

    service.suggest(jobs.Job(id=0, kind="llm"), [post_id], None, None, 4711)

    assert len(worker.requests) == 1, "one seed, one generation"
    assert len(worker.requests) == 1, "one seed, one generation"
    assert worker.requests[0]["seed"] == 4711
    row = db.get_connection().execute(
        "SELECT seed FROM llm_suggestions WHERE post_id=? LIMIT 1", (post_id,)
    ).fetchone()
    assert row["seed"] == 4711


def test_accepting_replaces_the_previously_accepted_suggestion():
    """Accepted proposals used to accumulate for ever and were invisible - the
    live database held 47 of them against 3 undecided. Only the one actually in
    the post is worth keeping; anything still undecided stays, because the user
    has not looked at it yet."""
    post_id = post_store.create(title="Post")
    first = _insert_suggestion(post_id, "title", "First title")
    second = _insert_suggestion(post_id, "title", "Second title")
    other_field = _insert_suggestion(post_id, "description", "A description")
    untouched = _insert_suggestion(post_id, "title", "Never decided")

    service.accept(first)
    service.accept(second)
    service.accept(other_field)

    left = {
        row["id"]: row["accepted"]
        for row in db.get_connection().execute(
            "SELECT id, accepted FROM llm_suggestions WHERE post_id=?", (post_id,)
        )
    }
    assert first not in left, "the superseded one is gone"
    assert left[second] == 1, "the last accepted title stays"
    assert left[other_field] == 1, "a different field keeps its own"
    assert left[untouched] == 0, "an undecided one is never touched"

    # Accepting the same row again removes nothing.
    service.accept(second)
    assert second in {
        row["id"]
        for row in db.get_connection().execute(
            "SELECT id FROM llm_suggestions WHERE post_id=?", (post_id,)
        )
    }


def test_a_reasoning_model_is_asked_for_an_answer_not_for_its_thoughts(monkeypatch):
    """A thinking model returns its reasoning in `thinking` and leaves `content`
    empty, so every request came back blank and the parser reported "no readable
    JSON". MiniCPM-V 4.5 failed nine of nine that way."""
    sent: list[dict] = []

    def fake_post(url, body, timeout):
        if url.endswith("/api/show"):
            return {"capabilities": ["completion", "vision", "thinking"]}
        sent.append(body)
        return {"message": {"content": "A title"}}

    monkeypatch.setattr(endpoint, "_post", fake_post)
    endpoint.Endpoint(None, "minicpm").generate({"user_content": "Name it."})

    assert sent[0]["think"] is False


def test_an_unreachable_server_names_the_address_and_the_fix():
    """An error says what to do next. A connection refused from urllib says only
    "Connection refused"."""
    hosted = endpoint.Endpoint("http://127.0.0.1:9", "whatever")
    with pytest.raises(endpoint.EndpointError) as raised:
        hosted.generate({"user_content": "x"})
    message = str(raised.value)
    assert "127.0.0.1:9" in message and "settings" in message

    # The picker must not raise for a server that is simply not running - and
    # "not running" is a different answer from "running, but empty".
    assert endpoint.list_models("http://127.0.0.1:9") is None


def test_profile_mutation_routes_create_edit_default_and_delete(client):
    last_seeded_sort_order = profiles.list_profiles()[-1]["sort_order"]
    created = client.post("/api/llm/profiles", json=_profile_payload()).json()
    assert created["name"] == "Custom"
    assert created["max_images"] == 3
    assert created["sort_order"] == last_seeded_sort_order + 1

    # The name is a label. Two profiles may carry the same one; the uuid is what
    # tells them apart, and the settings list shows it under the name.
    duplicate = client.post("/api/llm/profiles", json=_profile_payload())
    assert duplicate.status_code == 200
    assert duplicate.json()["name"] == created["name"]
    assert duplicate.json()["uuid"] != created["uuid"]
    assert duplicate.json()["sort_order"] == created["sort_order"] + 1

    edited = client.patch(
        f"/api/llm/profiles/{created['id']}",
        json={"name": "Custom edited", "include_images": False},
    ).json()
    assert (edited["name"], edited["include_images"]) == ("Custom edited", False)

    assert client.post(f"/api/llm/profiles/{created['id']}/default").json()["ok"] is True
    assert client.get("/api/llm/profiles").json()["default_id"] == str(created["id"])

    assert client.delete(f"/api/llm/profiles/{created['id']}").json()["ok"] is True
    assert db.get_setting("llm_default_profile_id") is None


def test_seeded_profiles_remain_owned_after_edit_delete_and_restart():
    seeded = profiles.list_profiles()
    edited_id = seeded[0]["id"]
    deleted = seeded[1]

    profiles.update(edited_id, {"name": "My standard"})
    assert profiles.delete(deleted["id"]) is True
    profiles.ensure_builtins()

    after_restart = profiles.list_profiles()
    assert profiles.get(edited_id)["name"] == "My standard"
    assert deleted["name"] not in {profile["name"] for profile in after_restart}


def test_a_builtin_is_its_uuid_not_its_name_or_its_place(client):
    """Rename every seed, shuffle every `sort_order`, and each one still answers
    with its own prompt. The predecessor matched the name and fell back to
    `sort_order` - a column the user can patch, whose meaning shifted the moment
    a fourth voice was added, so a renamed built-in got its neighbour's text."""
    seeded = profiles.list_profiles()
    by_uuid = {seed["uuid"]: seed for seed in profiles.BUILTINS}

    for index, profile in enumerate(seeded):
        profiles.update(
            profile["id"],
            {
                "name": f"Renamed {index}",
                "sort_order": 90 - index,      # reversed, so position lies too
                "system_prompt": "Maintainer's edit",
            },
        )

    for profile in seeded:
        response = client.get(f"/api/llm/profiles/{profile['id']}/seed")
        assert response.status_code == 200
        assert response.json()["system_prompt"] == by_uuid[profile["uuid"]]["system_prompt"]
        # Reading a seed never writes one.
        assert profiles.get(profile["id"])["system_prompt"] == "Maintainer's edit"
        assert profiles.get(profile["id"])["is_builtin"] is True

    custom = client.post("/api/llm/profiles", json=_profile_payload()).json()
    assert custom["uuid"] not in by_uuid
    assert custom["is_builtin"] is False
    response = client.get(f"/api/llm/profiles/{custom['id']}/seed")
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "profile_seed_not_found"


def test_restore_returns_a_deleted_builtin_and_leaves_the_users_own_alone(client):
    """The promise the button makes. Deleting a built-in is allowed, so without
    this there is no way back to a shipped profile except a fresh database. And
    the confirmation says *every* shipped value, so every column a seed owns is
    checked here - not just the text."""
    seeded = profiles.list_profiles()
    assert profiles.delete(seeded[-1]["id"]) is True
    profiles.update(
        seeded[0]["id"],
        {"system_prompt": "Maintainer's edit", "vision_max_side": 512, "max_images": 2},
    )
    mine = client.post("/api/llm/profiles", json=_profile_payload()).json()

    response = client.post("/api/llm/profiles/restore")
    assert response.status_code == 200
    assert response.json()["restored"] == [seed["name"] for seed in profiles.BUILTINS]

    restored = {profile["uuid"]: profile for profile in profiles.list_profiles()}
    for seed in profiles.BUILTINS:
        row = restored[seed["uuid"]]
        assert row["is_builtin"] is True
        for column in ("name", "system_prompt", "description", "temperature",
                       "max_new_tokens", "sort_order"):
            assert row[column] == seed[column], (seed["name"], column)
        for column, shipped in profiles.PROFILE_DEFAULTS.items():
            if column in seed:
                continue
            expected = bool(shipped) if column == "include_images" else shipped
            assert row[column] == expected, (seed["name"], column)

    assert restored[mine["uuid"]] == mine


def test_restore_resets_a_renamed_builtin_instead_of_adding_a_second(client):
    """What the UUID is for. Matching on the shipped name would leave a renamed
    row standing and put a fresh seed beside it - four built-ins become eight,
    and the old prompt keeps being offered."""
    seeded = profiles.list_profiles()
    for index, profile in enumerate(seeded):
        profiles.update(profile["id"], {"name": f"Mein Profil {index}"})

    assert client.post("/api/llm/profiles/restore").status_code == 200

    after = profiles.list_profiles()
    assert [profile["name"] for profile in after] == [
        seed["name"] for seed in profiles.BUILTINS
    ]
    assert [profile["uuid"] for profile in after] == [
        seed["uuid"] for seed in profiles.BUILTINS
    ]


def test_a_shared_name_never_blocks_a_restore(client):
    """The predecessor refused a restore whenever any row held a seed's name
    under another uuid - and told the user their own profile was in the way.
    Two built-ins with swapped names were enough to trigger it, with no user
    profile involved anywhere."""
    seeded = profiles.list_profiles()
    standard = next(row for row in seeded if row["name"] == "Standard")
    plain = next(row for row in seeded if row["name"] == "Plain")
    profiles.update(standard["id"], {"name": "Zebra"})
    profiles.update(plain["id"], {"name": "Standard"})

    # And a profile of the user's own on a seed's name, which is now allowed.
    mine = client.post("/api/llm/profiles", json=_profile_payload("Storyteller")).json()

    assert client.post("/api/llm/profiles/restore").status_code == 200

    restored = {row["uuid"]: row for row in profiles.list_profiles()}
    for seed in profiles.BUILTINS:
        assert restored[seed["uuid"]]["name"] == seed["name"], seed["name"]
    assert restored[mine["uuid"]] == mine, "the user's profile was not touched"
    assert sum(row["name"] == "Storyteller" for row in restored.values()) == 2


def test_the_last_profile_cannot_be_deleted(client):
    seeded = profiles.list_profiles()
    for profile in seeded[:-1]:
        assert client.delete(f"/api/llm/profiles/{profile['id']}").status_code == 200

    response = client.delete(f"/api/llm/profiles/{seeded[-1]['id']}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "last_profile"
    assert profiles.resolve(None)["id"] == seeded[-1]["id"]


def test_dismissing_removes_only_that_suggestion_and_does_not_edit_the_post(client):
    post_id = post_store.create(title="Owner title", detail="Owner detail")
    post_store.set_tags(post_id, ["owner tag"])
    dismissed_id = _insert_suggestion(post_id, "title", "Machine title")
    kept_id = _insert_suggestion(post_id, "description", "Machine detail")

    response = client.delete(f"/api/llm/suggestions/{dismissed_id}")

    assert response.status_code == 200
    assert [item["id"] for item in service.suggestions_for(post_id)] == [kept_id]
    post = post_store.get(post_id)
    assert (post["title"], post["detail"]) == ("Owner title", "Owner detail")
    assert [tag["name"] for tag in post_store.tags(post_id)] == ["owner tag"]


def test_dismissing_a_missing_suggestion_returns_the_same_404_as_accept(client):
    dismissed = client.delete("/api/llm/suggestions/9999")
    accepted = client.post("/api/llm/suggestions/9999/accept")

    assert dismissed.status_code == accepted.status_code == 404
    assert dismissed.json()["detail"] == accepted.json()["detail"] == "Suggestion not found"


def test_an_undismissed_suggestion_can_still_be_accepted(client):
    post_id = post_store.create(title="Owner title", detail="Owner detail")
    accepted_id = _insert_suggestion(post_id, "title", "Accepted title")
    dismissed_id = _insert_suggestion(post_id, "description", "Dismissed detail")
    assert client.delete(f"/api/llm/suggestions/{dismissed_id}").status_code == 200

    response = client.post(f"/api/llm/suggestions/{accepted_id}/accept")

    assert response.status_code == 200
    post = post_store.get(post_id)
    assert (post["title"], post["detail"]) == ("Accepted title", "Owner detail")
    rows = db.get_connection().execute(
        "SELECT id, accepted FROM llm_suggestions WHERE id IN (?, ?) ORDER BY id",
        (accepted_id, dismissed_id),
    ).fetchall()
    assert [(row["id"], row["accepted"]) for row in rows] == [(accepted_id, 1)]


def test_accepting_a_suggestion_refuses_a_pushing_post(client):
    post_id = post_store.create(title="Owned by a push")
    suggestion_id = _insert_suggestion(post_id, "title", "Machine title")
    post_store.set_fields(post_id, state=lifecycle.PUSHING)

    response = client.post(f"/api/llm/suggestions/{suggestion_id}/accept")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "post_push_in_progress"
    assert post_store.get(post_id)["title"] == "Owned by a push"
    assert service.suggestions_for(post_id)[0]["accepted"] == 0


def test_lora_tags_and_weights_are_stripped_before_the_model_sees_them():
    """A model shown `<lora:MoonTastic:0.8>` will happily put "moontastic" in
    the tags - which is exactly what the prompt forbids."""
    cleaned = prompts.clean_prompt(
        "masterpiece, (best quality:1.2), <lora:MoonTastic:0.8>, BREAK, a1b2c3d4e5f6, cute girl"
    )
    assert "lora" not in cleaned.lower()
    assert "moontastic" not in cleaned.lower()
    assert "1.2" not in cleaned
    assert "a1b2c3d4e5f6" not in cleaned
    assert "cute girl" in cleaned


def test_excluded_tags_are_dropped():
    cleaned = prompts.clean_prompt("masterpiece, cute girl", exclude_tags=["masterpiece"])
    assert cleaned == "cute girl"


def test_a_reasoning_preamble_is_stripped():
    """Qwen-family models narrate before answering; the JSON is still in there."""
    raw = (
        "<think>Let me consider the subject.</think>\n"
        "Here is the JSON:\n"
        '```json\n{"title":"Rain On Neon Streets","tags":["neon","rain"]}\n```'
    )
    result = prompts.parse_result(raw, title_max_words=7, tag_count=7)
    assert result["title"] == "Rain On Neon Streets"
    assert result["tags"] == ["neon", "rain"]


def test_a_bare_json_object_works_too():
    result = prompts.parse_result(
        '{"title":"A","description":"B","tags":["c"]}', title_max_words=7, tag_count=7
    )
    assert (result["title"], result["description"]) == ("A", "B")


def test_the_title_is_cut_to_the_configured_length():
    raw = '{"title":"One Two Three Four Five Six Seven Eight Nine","tags":["a1","b2","c3"]}'
    assert len(prompts.parse_result(raw, title_max_words=4, tag_count=7)["title"].split()) == 4


def test_tags_are_lowercased_deduplicated_and_bounded():
    raw = '{"title":"T","tags":["Portrait","portrait"," RAIN ","a","x"]}'
    result = prompts.parse_result(raw, title_max_words=7, tag_count=2)
    assert result["tags"] == ["portrait", "rain"]  # "a" too short, "x" too short, cut to 2


def test_a_description_preamble_is_stripped():
    raw = '{"title":"T","description":"Here is: A girl in the rain.","tags":["rain"]}'
    result = prompts.parse_result(raw, title_max_words=7, tag_count=7)
    assert result["description"] == "A girl in the rain."


def test_unparsable_output_raises_rather_than_inventing_a_title():
    with pytest.raises(ValueError):
        prompts.parse_result("I cannot help with that.", title_max_words=7, tag_count=7)


def test_the_user_prompt_contains_only_data_profile_numbers_and_wire_format():
    content = prompts.build_user_content(
        ["a cat"], title_max_words=7, tag_count=5, hint="Look toward the doorway.", sheet=True
    )
    lowered = content.casefold()
    moved_to_profile = (
        "evocative",
        "memorable",
        "central subject",
        "masterpiece",
        "lowercase",
        "lora",
        "checkpoint",
        "trigger token",
        "quality word",
        "model name",
        "mature content",
        "moral commentary",
        "one to three",
        "underscore",
        "keep every rule above",
    )
    for instruction in moved_to_profile:
        assert instruction not in lowered

    assert "a cat" in content
    assert "Maximum title words: 7" in content
    assert "Number of tags: 5" in content
    assert "Answer with one JSON object and nothing else" in content
    assert '"title":"..."' in content
    assert "contact sheet containing every picture" in content
    assert content.endswith("Look toward the doorway.")


def test_an_empty_prompt_list_still_produces_a_usable_instruction():
    content = prompts.build_user_content([], title_max_words=7, tag_count=5)
    assert "No generation prompt is available" in content
    assert "images" in content


def test_seeded_prompts_are_standalone_and_not_prefix_variants():
    seeded = [profile["system_prompt"] for profile in profiles.BUILTINS]
    assert len(set(seeded)) == len(profiles.BUILTINS)
    assert all("spaces, never underscores" in prompt for prompt in seeded)
    for index, prompt in enumerate(seeded):
        for other in seeded[index + 1 :]:
            assert not prompt.startswith(other)
            assert not other.startswith(prompt)


def test_each_seed_description_treats_a_picture_set_as_one_post():
    """The contact sheet is one image of many pictures, and a model left to
    itself walks it frame by frame - "in the first picture ..., while the
    second ...". Every voice carries the rule, so a fifth profile written
    without it fails here."""
    for seed in profiles.BUILTINS:
        prompt = seed["system_prompt"].casefold()
        assert "one description for the whole post" in prompt, seed["name"]
        assert "picture-by-picture" in prompt, seed["name"]


def test_each_seed_gives_title_casing_only_to_the_title_and_tags():
    for seed in profiles.BUILTINS:
        paragraphs = [part.casefold() for part in seed["system_prompt"].split("\n\n")]
        title_paragraph = paragraphs[1]
        assert "capital letter" in title_paragraph
        assert "sentence case" in title_paragraph
        assert "names" in title_paragraph and "franchises" in title_paragraph
        lowercase_paragraphs = [
            index for index, paragraph in enumerate(paragraphs) if "lowercase" in paragraph
        ]
        assert lowercase_paragraphs == [3]


def test_encoding_images_never_touches_the_original(fixture_images):
    """The downscaled copies are for the model only; the uploader always sends
    the file from disk."""
    _, images = fixture_images
    before = images[0].read_bytes()
    encoded = prompts.encode_images([images[0]], max_side=256)
    assert len(encoded) == 1
    assert images[0].read_bytes() == before


def _tiles(tmp_path, count, size=(300, 400)):
    from PIL import Image

    tmp_path.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        path = tmp_path / f"tile{index}.png"
        Image.new("RGB", size, (index * 20 % 255, 40, 80)).save(path)
        paths.append(path)
    return paths


def _sheet_size(paths, tile_side=512):
    import base64
    import io

    from PIL import Image

    encoded = prompts.build_contact_sheet(paths, tile_side=tile_side)
    if encoded is None:
        return None
    return Image.open(io.BytesIO(base64.b64decode(encoded))).size


def test_every_picture_of_the_post_is_on_the_sheet(tmp_path):
    """The sheet used to stop at nine and say nothing. With `max_images` at 1 it
    is the only thing the model ever sees, so a tenth picture that is not on it
    does not exist for the suggestion."""
    # 12 pictures: 4 columns, 3 rows, at the configured tile size.
    width, height = _sheet_size(_tiles(tmp_path, 12))
    assert width == 4 * 512 + 5 * 6
    assert height == 3 * 512 + 4 * 6


def test_the_tile_size_does_not_shrink_as_the_post_grows(tmp_path):
    """`vision_max_side` is the size of one picture in the grid. It used to be a
    budget for the whole sheet divided by the column count, so a five-image post
    showed each picture a third smaller than a four-image one - the setting did
    not mean what its label said."""
    four = _sheet_size(_tiles(tmp_path / "a", 4))
    five = _sheet_size(_tiles(tmp_path / "b", 5))

    assert four == (2 * 512 + 3 * 6, 2 * 512 + 3 * 6)
    # Five needs a third column, so the sheet widens; the rows stay as tall.
    assert five == (3 * 512 + 4 * 6, 2 * 512 + 3 * 6)
    assert five[1] == four[1], "the tile is unchanged, only the grid is wider"


def test_a_single_picture_gets_no_grid(tmp_path):
    """A grid of one is the picture with a border around it."""
    assert prompts.build_contact_sheet(_tiles(tmp_path, 1), tile_side=512) is None


def test_an_unreadable_image_is_skipped_not_fatal(tmp_path):
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"nope")
    assert prompts.encode_images([broken], max_side=256) == []


class _Loaded:
    """Stands in for whatever `from_pretrained` hands back."""

    device = "cpu"

    def __init__(self, can_generate: bool = True):
        self._can_generate = can_generate

    def can_generate(self):
        return self._can_generate

    def eval(self):
        return self


class _Recorder:
    """A from_pretrained that remembers how it was called."""

    def __init__(self, can_generate: bool = True):
        self.calls: list[dict] = []
        self._can_generate = can_generate

    def from_pretrained(self, model_dir, **options):
        self.calls.append(options)
        return _Loaded(self._can_generate)


class _Failing:
    """A class that is present but refuses this checkpoint."""

    def __init__(self):
        self.calls: list[dict] = []

    def from_pretrained(self, model_dir, **options):
        self.calls.append(options)
        raise ValueError("this checkpoint is not for me")


def _transformers(monkeypatch, **classes):
    """The test environment has no torch; both are injected as namespaces."""
    monkeypatch.setitem(
        sys.modules,
        "transformers",
        types.SimpleNamespace(__version__="5.16.1", **classes),
    )
    monkeypatch.setitem(sys.modules, "torch", types.SimpleNamespace(bfloat16="bfloat16"))


@pytest.mark.parametrize("passed, expected", [({}, False), ({"trust_remote_code": False}, False),
                                              ({"trust_remote_code": True}, True)])
def test_a_model_directory_runs_its_own_code_only_when_told_to(monkeypatch, passed, expected):
    """Both loaders used to pass trust_remote_code=True unconditionally, which
    turned picking a model folder into running its Python as the maintainer."""
    processor, tokenizer, causal = _Recorder(), _Recorder(), _Recorder()
    _transformers(
        monkeypatch,
        AutoProcessor=processor,
        AutoTokenizer=tokenizer,
        AutoModelForCausalLM=causal,
    )

    model_loader.load_model("/models/anything", quantise=False, **passed)

    assert [call["trust_remote_code"] for call in processor.calls] == [expected]
    assert [call["trust_remote_code"] for call in causal.calls] == [expected]


@pytest.mark.parametrize(
    "device_map, offloaded",
    [
        ({"model.layers.0": 0, "model.layers.30": 0}, False),
        ({"model.layers.0": 0, "model.layers.30": "cpu"}, True),
        ({"model.layers.0": 0, "model.layers.30": "disk"}, True),
        (None, False),
    ],
)
def test_a_model_that_did_not_fit_the_card_says_so(device_map, offloaded):
    """The whole point of the offload permission is that a checkpoint too big
    for the GPU still runs - slowly. Slowly and silently is the bad outcome, so
    the flag that drives the job line and the settings page is checked here: no
    oversized checkpoint is on this machine to prove it by loading one.
    """
    assert model_loader._offloaded(device_map) is offloaded


def test_the_job_line_says_when_the_model_is_partly_on_the_cpu():
    fitted = types.SimpleNamespace(info={"device": "cuda:0", "offloaded": False})
    spilled = types.SimpleNamespace(info={"device": "cuda:0", "offloaded": True})

    assert service.device_label(fitted) == "cuda:0"
    assert "CPU" in service.device_label(spilled)


def test_a_fresh_profile_starts_at_the_measured_defaults():
    """A new install and a migrated one must agree. The defaults live in the
    schema, in ProfileUpsert, in PROFILE_DEFAULTS - which is what `create` falls
    back to and what a restored built-in is set to - and in the panel's draft;
    a migration moves the old ones. Changing one and forgetting another leaves
    two kinds of installation quietly different."""
    from backend.models import ProfileUpsert

    made = profiles.create({"name": "Fresh", "system_prompt": "Answer in JSON."})
    assert (made["vision_max_side"], made["max_images"]) == (768, 1)

    bounds = ProfileUpsert(name="x", system_prompt="y")
    assert (bounds.vision_max_side, bounds.max_images) == (768, 1)

    shipped = profiles.PROFILE_DEFAULTS
    assert (shipped["vision_max_side"], shipped["max_images"]) == (768, 1)

    for built_in in profiles.list_profiles():
        assert built_in["vision_max_side"] == 768, built_in["name"]
        assert built_in["max_images"] == 1, built_in["name"]


def test_every_patchable_profile_field_has_a_bound():
    """PATCH filters by `_PATCHABLE` and validates by `ProfilePatch`. A key in
    one and not the other is a silent no-op: the request passes validation, the
    field is dropped, and the caller gets 200 with nothing changed."""
    from backend.models import ProfilePatch

    assert set(ProfilePatch.model_fields) == profiles._PATCHABLE


def test_a_class_that_loads_but_cannot_generate_is_not_the_answer(monkeypatch):
    """`AutoModel` comes before `AutoModelForCausalLM` in the chain and loads a
    plain text checkpoint happily - transformers 5 keeps generate() on
    GenerationMixin, so `Qwen2Model.can_generate()` is False. Accepting that
    class would produce a model with no generate() and a confusing failure at
    the first request instead of at load time.
    """
    headless, causal = _Recorder(can_generate=False), _Recorder()
    _transformers(
        monkeypatch,
        AutoProcessor=_Failing(),
        AutoTokenizer=_Recorder(),
        AutoModel=headless,
        AutoModelForCausalLM=causal,
    )

    _model, _processor, _tokenizer, _vision, _error, diagnostics = model_loader.load_model(
        "/models/text-only", quantise=False
    )

    assert len(headless.calls) == 1, "the headless class was tried"
    assert len(causal.calls) == 1, "and the chain went on to the next name"
    assert diagnostics["resolved_model_class"] == "AutoModelForCausalLM"
    assert "AutoModel: loaded but cannot generate" in diagnostics["class_failures"]
    assert "AutoModelForVision2Seq: unavailable in transformers 5.16.1" in (
        diagnostics["class_failures"]
    )


def test_an_already_quantised_checkpoint_is_not_quantised_again(monkeypatch, tmp_path):
    """An AWQ, GPTQ or compressed-tensors checkpoint carries its own
    quantization_config. Wrapping a second one around it is how such a download
    fails on load.
    """
    quantised = tmp_path / "prequantised"
    quantised.mkdir()
    (quantised / "config.json").write_text('{"quantization_config": {"bits": 4}}')
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "config.json").write_text('{"architectures": ["Qwen2ForCausalLM"]}')

    for checkpoint, expected in ((quantised, False), (plain, True)):
        causal = _Recorder()
        _transformers(
            monkeypatch,
            AutoProcessor=_Failing(),
            AutoTokenizer=_Recorder(),
            AutoModelForCausalLM=causal,
            BitsAndBytesConfig=dict,
        )

        model_loader.load_model(str(checkpoint), quantise=True)

        config = causal.calls[0].get("quantization_config")
        assert bool(config) is expected
        if expected:
            # Without this flag bitsandbytes refuses instead of offloading the
            # moment device_map="auto" wants the CPU, which is what stops a model
            # that does not quite fit an 8 GB card from running at all.
            assert config["llm_int8_enable_fp32_cpu_offload"] is True


def test_the_worker_is_told_to_trust_model_code_only_when_the_setting_is_on(monkeypatch, tmp_path):
    interpreter = tmp_path / "python"
    interpreter.write_text("")
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    db.set_setting("llm_python", str(interpreter))
    db.set_setting("llm_model_dir", str(model_dir))

    started: list[dict] = []

    class FakeWorker:
        alive = True
        info: dict = {}

        def __init__(self, python, options):
            started.append(options)

    monkeypatch.setattr(service, "Worker", FakeWorker)
    monkeypatch.setattr(service, "_worker", None)

    service.start_worker()
    monkeypatch.setattr(service, "_worker", None)
    db.set_setting("llm_trust_remote_code", "1")
    service.start_worker()

    assert [options["trust_remote_code"] for options in started] == [False, True]


def test_non_thinking_generation_uses_the_recommended_presence_penalty():
    class Inputs(dict):
        def __init__(self):
            super().__init__(input_ids=types.SimpleNamespace(shape=(1, 1)))

        def to(self, _device):
            return self

    class Tokenizer:
        chat_template = None

        def __call__(self, _prompt, return_tensors):
            assert return_tensors == "pt"
            return Inputs()

        def decode(self, _tokens, skip_special_tokens):
            assert skip_special_tokens is True
            return "done"

    class Model:
        device = "cpu"

        def __init__(self):
            self.kwargs = {}

        def generate(self, **kwargs):
            self.kwargs = kwargs
            return [[0, 1]]

    model = Model()
    torch = types.SimpleNamespace(inference_mode=nullcontext)
    worker.generate(
        {"system_prompt": "System", "user_content": "User", "temperature": 0.7},
        model,
        None,
        Tokenizer(),
        False,
        torch,
    )

    processors = model.kwargs["logits_processor"]
    assert len(processors) == 1
    assert processors[0].penalty == 1.5


@pytest.mark.parametrize(
    ("platform", "relative_path"),
    [
        ("linux", ("bin", "python")),
        ("win32", ("Scripts", "python.exe")),
    ],
)
def test_only_the_project_and_this_machine_are_probed(
    tmp_path, monkeypatch, platform, relative_path
):
    """The fallback list used to name venvs of unrelated projects on this one
    machine; a stale environment could be picked up silently. What stays is the
    environment this project creates itself, plus the two obvious interpreters.
    """
    own = tmp_path.joinpath(".venv-llm", *relative_path)
    own.parent.mkdir(parents=True)
    own.write_text("")
    monkeypatch.setattr(service.config, "project_root", lambda: tmp_path)
    monkeypatch.setattr(service.sys, "platform", platform)
    probed: list[str] = []
    monkeypatch.setattr(service, "_detected", None)
    monkeypatch.setattr(service, "_has_torch", lambda candidate: bool(probed.append(candidate)))
    monkeypatch.setattr(
        service.shutil, "which", lambda name: "/usr/bin/python3" if name == "python3" else None
    )

    assert service.detect_python() is None
    assert probed == [str(own), sys.executable, "/usr/bin/python3"]


def test_a_checkout_without_its_own_llm_environment_probes_two(tmp_path, monkeypatch):
    """`webui.sh --llm` may never have run. Then there is nothing to try."""
    monkeypatch.setattr(service.config, "project_root", lambda: tmp_path)
    probed: list[str] = []
    monkeypatch.setattr(service, "_detected", None)
    monkeypatch.setattr(service, "_has_torch", lambda candidate: bool(probed.append(candidate)))
    monkeypatch.setattr(
        service.shutil, "which", lambda name: "/usr/bin/python3" if name == "python3" else None
    )

    assert service.detect_python() is None
    assert probed == [sys.executable, "/usr/bin/python3"]


def test_the_first_interpreter_with_torch_wins(monkeypatch):
    monkeypatch.setattr(service, "_detected", None)
    monkeypatch.setattr(service, "_has_torch", lambda candidate: candidate == sys.executable)
    assert service.detect_python() == sys.executable


def test_a_remembered_result_for_another_interpreter_is_not_shown(tmp_path, monkeypatch):
    """The maintainer chased a missing Pillow that a ten-day-old run had
    reported for an interpreter they had since replaced."""
    from backend import db

    interpreter = tmp_path / "python"
    interpreter.write_text("")
    model_dir = tmp_path / "model"
    model_dir.mkdir()
    db.set_setting("llm_python", str(interpreter))
    db.set_setting("llm_model_dir", str(model_dir))
    db.set_json_setting(
        "llm_last_ready",
        {
            "vision": False,
            "vision_error": "requires the PIL library",
            "device": "cuda:0",
            "python": "/gone/.venv-llm/bin/python",
            "model_dir": str(model_dir),
            "at": "2026-08-20T23:21:31Z",
        },
    )

    state = service.status()

    assert state["vision"] is None
    assert state["vision_error"] is None
    assert state["resolved_device"] is None
    assert state["ready"] is True, "the setup itself is fine; only the memory was stale"


def test_nothing_configured_is_not_ready_and_says_what_to_set(monkeypatch):
    """With no default model folder left, an unset one must not read as the
    current directory - Path("") is a directory that exists."""
    monkeypatch.setattr(service, "_detected", None)
    monkeypatch.setattr(service, "_has_torch", lambda candidate: False)

    state = service.status()
    assert (state["python"], state["model_dir"]) == (None, "")
    assert state["ready"] is False

    with pytest.raises(service.LlmError) as interpreter_error:
        service.start_worker()
    assert "settings" in str(interpreter_error.value)


def test_a_missing_model_folder_names_the_setting_to_fill_in(monkeypatch, tmp_path):
    interpreter = tmp_path / "python"
    interpreter.write_text("")
    db.set_setting("llm_python", str(interpreter))

    with pytest.raises(service.LlmError) as error:
        service.start_worker()
    message = str(error.value)
    assert "model folder" in message and "settings" in message


# A real answer from the model comparison of 2026-09-03: the keys are quoted,
# the values are not, and the list items are not either. Ministral-3B produced
# this shape in 16 of 20 cells.
_MINISTRAL_ANSWER = """```json
{
  "title": "jaguar near water in dense jungle",
  "description": The images feature a jaguar in various poses near a reflective
water body surrounded by lush, tropical foliage.

  "tags": [
    jaguar,
    jungle,
    water
  ],
  "reason": The images collectively depict a single jaguar.
}
```"""


def test_an_answer_that_is_almost_json_still_yields_a_suggestion():
    """Without the repair this whole answer was discarded, and with it a model
    that sees perfectly well looks unusable: the animal is named correctly here,
    only the quotation marks are missing."""
    parsed = prompts.parse_result(_MINISTRAL_ANSWER, title_max_words=7, tag_count=7)

    assert parsed["title"] == "jaguar near water in dense jungle"
    assert parsed["tags"] == ["jaguar", "jungle", "water"]
    assert "jaguar" in parsed["description"]


def test_a_repair_that_guesses_the_wrong_shape_yields_no_description():
    """The repair is a second guess, not an authority. One of three answers
    checked by hand came back with the description as a list of strings; a
    description made of brackets is worse than none."""
    answer = '{"title": "Vier Portraits", "description": [broken, list], "tags": [a, b]}'

    parsed = prompts.parse_result(answer, title_max_words=7, tag_count=7)

    assert parsed["title"] == "Vier Portraits"
    assert parsed["description"] == "", parsed["description"]


def test_a_valid_answer_never_reaches_the_repair(monkeypatch):
    """An answer that parses today must take exactly the path it takes today -
    a fallback that quietly starts handling the normal case is how a parser
    stops being predictable."""
    called = []
    monkeypatch.setattr(prompts, "_repaired", lambda text: called.append(text))

    parsed = prompts.parse_result(
        '{"title": "Ein Titel", "description": "Ein Satz.", "tags": ["eins"]}',
        title_max_words=7, tag_count=7)

    assert parsed["title"] == "Ein Titel"
    assert called == [], "the repair was reached for an answer that was already valid"


def test_only_the_suggestion_asks_for_a_schema(monkeypatch):
    """Prompt shortening shares this worker and wants one sentence back. Forcing
    the suggestion schema on every hosted request handed the bulk-edit planner a
    JSON object as the shortened prompt - and nothing would have complained."""
    sent: list[dict] = []

    def fake_post(url, body, timeout):
        if url.endswith("/api/show"):
            return {"capabilities": ["completion", "vision"]}
        sent.append(body)
        return {"message": {"content": "ok"}}

    monkeypatch.setattr(endpoint, "_post", fake_post)
    hosted = endpoint.Endpoint(None, "ministral-3b")

    hosted.generate({"user_content": "Shorten this.", "system_prompt": "S"})
    hosted.generate({"user_content": "Describe.", "system_prompt": "S",
                     "response_schema": endpoint.ANSWER_SCHEMA})

    assert "format" not in sent[0], "shortening must not be constrained to the schema"
    assert sent[1]["format"] == endpoint.ANSWER_SCHEMA


def test_a_seeded_request_sends_its_complete_ollama_chat_body(monkeypatch):
    sent: list[dict] = []

    def fake_post(url, body, timeout):
        if url.endswith("/api/show"):
            return {"capabilities": ["completion"]}
        sent.append(body)
        return {"message": {"content": "Answer"}}

    monkeypatch.setattr(endpoint, "_post", fake_post)
    hosted = endpoint.Endpoint(None, "ministral-3b")

    hosted.generate(
        {
            "system_prompt": "System instruction.",
            "user_content": "Describe this.",
            "max_new_tokens": 512,
            "temperature": 0.7,
            "top_p": 0.9,
            "top_k": 30,
            "seed": "12345",
        }
    )

    assert sent == [
        {
            "model": "ministral-3b",
            "messages": [
                {"role": "system", "content": "System instruction."},
                {"role": "user", "content": "Describe this."},
            ],
            "options": {
                "num_predict": 512,
                "temperature": 0.7,
                "top_p": 0.9,
                "top_k": 30,
                "seed": 12345,
                "num_ctx": endpoint.DEFAULT_NUM_CTX,
            },
            "stream": False,
            "think": False,
        }
    ]


def test_a_picture_that_cannot_be_measured_buys_the_larger_window():
    """Guessing low loses the answer mid-word with nothing said; guessing high
    costs about a fifth of the time. An unreadable picture must not be counted
    as no picture."""
    assert endpoint.context_window([], 500) == endpoint.DEFAULT_NUM_CTX
    assert endpoint.context_window(["not base64 at all"], 500) == endpoint.UNMEASURED_NUM_CTX


def test_a_repair_that_yields_nothing_is_still_a_failure():
    """`repair_json` answers a lone brace with an empty object, which parses.
    Left alone the run reported success, wrote no suggestion and said nothing."""
    with pytest.raises(ValueError):
        prompts.parse_result("{", title_max_words=7, tag_count=7)


class _SeeingWorker(_ScriptedWorker):
    """A worker that reports vision, the way a real one does after loading."""

    def __init__(self, answers, sees=True):
        super().__init__(answers)
        self.info = {"device": "cuda", "vision": sees}


def _material_request(client, scanned, monkeypatch, *, sees, material="auto"):
    post_id = client.post("/api/posts", json={"image_ids": scanned[:1]}).json()["id"]
    db.set_setting("llm_variants", "1")
    worker = _SeeingWorker([_answer("T", "D", ["a"])], sees=sees)
    monkeypatch.setattr(service, "start_worker", lambda: worker)
    monkeypatch.setattr(service, "stop_worker", lambda **_kwargs: None)
    service.suggest(jobs.Job(id=0, kind="llm"), [post_id], None, None, material=material)
    return worker.requests[0]


def test_a_model_that_sees_gets_the_pictures_and_not_the_prompt(
    client, scanned, monkeypatch
):
    """Measured 2026-09-02 across eight models: with the generation prompt beside
    the pictures 2 of 8 named the animal in one correctly, without it 6 of 8. The
    prompt gets copied instead of the picture being read, so a seeing model must
    not receive both."""
    request = _material_request(client, scanned, monkeypatch, sees=True)

    assert request["images"], "a seeing model gets the pictures"
    assert "No generation prompt is available" in request["user_content"]


def test_a_model_that_cannot_see_gets_the_prompt(client, scanned, monkeypatch):
    """The other half of the same rule. Without it a text-only model would be
    asked to describe pictures it never receives."""
    request = _material_request(client, scanned, monkeypatch, sees=False)

    assert not request["images"], "no pictures to a model that cannot see them"
    assert "GENERATION PROMPTS:" in request["user_content"]
    assert "No generation prompt is available" not in request["user_content"]


def test_asking_for_the_prompt_overrides_a_seeing_model(client, scanned, monkeypatch):
    """The per-run override. `auto` is a rule, not a cage: a user comparing the
    two must be able to ask for one of them."""
    request = _material_request(client, scanned, monkeypatch, sees=True, material="prompt")

    assert not request["images"]
    assert "No generation prompt is available" not in request["user_content"]
