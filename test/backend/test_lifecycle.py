"""The state machine, and the three CivitAI rules that cannot be undone.

Each of these is a rule the platform does not enforce the way one would hope:
a wrong publishedAt is not refused but acted on, a published post cannot be
taken back, and a binding "update" is accepted and ignored. So they are enforced
here instead.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.posts import lifecycle
from backend.store import posts as post_store


@pytest.fixture
def post_id():
    return post_store.create(title="Test")


def test_a_published_post_cannot_be_scheduled_again(post_id):
    """CivitAI: "You cannot reschedule a post that is already published"."""
    post_store.set_fields(post_id, state=lifecycle.PUBLISHED)
    assert not lifecycle.can_transition(lifecycle.PUBLISHED, lifecycle.SCHEDULED)
    with pytest.raises(lifecycle.TransitionError):
        lifecycle.transition(post_id, lifecycle.SCHEDULED)


def test_there_is_no_way_back_to_an_unpublished_remote_draft(post_id):
    """CivitAI has no unpublish for posts; deletion is the only route back."""
    for state in (lifecycle.SCHEDULED, lifecycle.PUBLISHED):
        assert not lifecycle.can_transition(state, lifecycle.REMOTE_DRAFT)


def test_deletion_is_the_route_back_to_a_local_draft():
    for state in (lifecycle.SCHEDULED, lifecycle.PUBLISHED, lifecycle.REMOTE_DRAFT):
        assert lifecycle.can_transition(state, lifecycle.DRAFT)


def test_guard_reschedule_refuses_a_published_post():
    with pytest.raises(lifecycle.TransitionError):
        lifecycle.guard_reschedule({"state": lifecycle.PUBLISHED})


def test_guard_reschedule_refuses_a_time_that_has_already_passed():
    """Even when the local state still says "scheduled": the remote timestamp is
    the authority."""
    with pytest.raises(lifecycle.TransitionError):
        lifecycle.guard_reschedule(
            {"state": lifecycle.SCHEDULED, "remote_published_at": "2020-01-01T00:00:00Z"}
        )


def test_guard_reschedule_allows_a_future_time():
    lifecycle.guard_reschedule(
        {"state": lifecycle.SCHEDULED, "remote_published_at": "2099-01-01T00:00:00Z"}
    )


def test_the_binding_cannot_change_once_civitai_holds_one():
    """post.update has no modelVersionId field, so an "update" would be accepted
    and silently ignored - the worst kind of failure."""
    with pytest.raises(lifecycle.TransitionError) as info:
        lifecycle.guard_binding_change({"bound_model_version_id": 111}, 222)
    assert "rebuild" in str(info.value)


def test_the_binding_may_be_set_while_the_post_is_still_local():
    lifecycle.guard_binding_change({"bound_model_version_id": None}, 222)


def test_setting_the_same_binding_again_is_not_a_change():
    lifecycle.guard_binding_change({"bound_model_version_id": 111}, 111)


def test_an_unknown_state_is_refused(post_id):
    with pytest.raises(lifecycle.TransitionError):
        lifecycle.transition(post_id, "erfunden")


def test_every_state_has_presentation_metadata():
    assert set(lifecycle.INFO) == lifecycle.ALL_STATES


def test_only_states_that_have_a_remote_can_be_edited_remotely():
    for state, info in lifecycle.INFO.items():
        if info.can_edit_remote:
            assert info.has_remote, f"{state} claims remote editing without a remote post"


def test_every_transition_error_code_exists_in_both_catalogues():
    i18n = Path(__file__).resolve().parents[2] / "frontend" / "src" / "i18n"
    for language in ("en", "de"):
        catalogue = json.loads((i18n / f"{language}.json").read_text(encoding="utf-8"))
        assert {f"error.{code}" for code in lifecycle.TRANSITION_ERROR_CODES} <= set(
            catalogue
        )


def test_nothing_that_was_never_published_can_be_archived():
    """`ARC-01`. The archive is the record of what went out, nothing else.

    A plan that was given up on is deleted - "would then never have been a post
    at all" - so draft, ready and failed have no way into the archive.
    """
    from backend.posts import lifecycle

    for state in (lifecycle.DRAFT, lifecycle.READY, lifecycle.FAILED):
        assert lifecycle.ARCHIVED not in lifecycle.TRANSITIONS[state], state
        assert not lifecycle.can_transition(state, lifecycle.ARCHIVED)
    assert lifecycle.can_transition(lifecycle.PUBLISHED, lifecycle.ARCHIVED)


def test_a_remote_missing_post_is_archivable_only_with_a_publication_date(client, scanned):
    """The one state that can mean either.

    Live and lost by CivitAI belongs in the archive; a remote draft deleted
    before it ever went out does not, and the publication date separates them.
    """
    from backend.posts import lifecycle
    from backend.store import posts as post_store

    never = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    post_store.set_fields(never, state=lifecycle.REMOTE_MISSING)
    with pytest.raises(lifecycle.TransitionError) as refused:
        lifecycle.transition(never, lifecycle.ARCHIVED)
    assert refused.value.code == lifecycle.ERROR_ARCHIVE_UNPUBLISHED
    assert post_store.get(never)["state"] == lifecycle.REMOTE_MISSING

    was_live = client.post("/api/posts", json={"image_ids": [scanned[1]]}).json()["id"]
    post_store.set_fields(
        was_live,
        state=lifecycle.REMOTE_MISSING,
        remote_published_at="2026-08-20T10:00:00Z",
    )
    assert lifecycle.transition(was_live, lifecycle.ARCHIVED) == lifecycle.ARCHIVED


def test_a_post_deleted_before_it_went_live_is_not_archivable(client, scanned):
    """A future publish time is not proof of publication (`ARC-01`).

    A post scheduled on CivitAI and deleted there before it went live becomes
    `remote_missing` with its future date intact. Testing only that the date is
    set would let exactly the case the guard exists for through.
    """
    from datetime import timedelta

    from backend.posts import lifecycle, schedule
    from backend.store import posts as post_store

    future = schedule.iso_z(schedule.utcnow() + timedelta(days=3))
    post = client.post("/api/posts", json={"image_ids": [scanned[0]]}).json()["id"]
    post_store.set_fields(post, state=lifecycle.REMOTE_MISSING, remote_published_at=future)

    with pytest.raises(lifecycle.TransitionError) as refused:
        lifecycle.transition(post, lifecycle.ARCHIVED)
    assert refused.value.code == lifecycle.ERROR_ARCHIVE_UNPUBLISHED
