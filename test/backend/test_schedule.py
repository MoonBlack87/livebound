"""The scheduling rules.

These are the tests that matter most in this project. CivitAI answers a
publishedAt that is too close by publishing immediately rather than by refusing,
and there is no unpublish - so a bug here is not a wrong error message, it is a
post the user never meant to make public.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend import config
from backend.posts import schedule

NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def test_too_soon_is_rejected():
    check = schedule.validate(NOW + timedelta(minutes=59), now=NOW)
    assert not check.ok
    assert check.code == "too_soon"


def test_the_sixty_minute_boundary_is_the_floor():
    assert schedule.validate(NOW + timedelta(minutes=60), now=NOW).ok
    assert schedule.earliest_publish(NOW) == NOW + timedelta(minutes=60)


def test_more_than_three_months_ahead_is_rejected():
    check = schedule.validate(schedule.add_months(NOW, 4), now=NOW)
    assert not check.ok
    assert check.code == "too_far"


def test_missing_time_is_rejected():
    assert schedule.validate(None, now=NOW).code == "missing"


def test_add_months_clamps_the_day():
    """31 January + 1 month is the 28th, not an invalid date."""
    result = schedule.add_months(datetime(2026, 1, 31, tzinfo=timezone.utc), 1)
    assert (result.year, result.month, result.day) == (2026, 2, 28)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("90", 90),
        ("1h30", 90),
        ("3h 20min", 200),
        ("2d 4h", 3120),
        ("2,5h", 150),
        ("", None),
        ("tomorrow", None),
    ],
)
def test_offset_parsing(text, expected):
    assert schedule.parse_offset(text) == expected


def test_a_bare_number_means_minutes():
    """The first thing anyone types is a number. Reading it as hours would
    schedule the post two days late."""
    assert schedule.parse_offset("45") == 45


def test_relative_offsets_below_the_floor_are_raised_not_rejected():
    """"In 30 minutes" is a clear intent; the honest answer is the soonest
    CivitAI allows, not a failed push."""
    resolved = schedule.resolve(
        mode=schedule.RELATIVE, offset_minutes=30, scheduled_at=None, now=NOW
    )
    assert resolved == schedule.earliest_publish(NOW)
    assert schedule.validate(resolved, now=NOW).ok


def test_relative_offsets_above_the_floor_are_kept():
    resolved = schedule.resolve(
        mode=schedule.RELATIVE, offset_minutes=200, scheduled_at=None, now=NOW
    )
    assert resolved == NOW + timedelta(minutes=200)


def test_a_relative_plan_cannot_go_stale():
    """The closing step resolves from its own clock, after the upload."""
    later = NOW + timedelta(days=9)
    resolved = schedule.resolve(
        mode=schedule.RELATIVE, offset_minutes=90, scheduled_at=None, now=later
    )
    assert schedule.validate(resolved, now=later).ok


def test_an_absolute_plan_does_go_stale():
    """The counterpart, and the reason relative is the default."""
    fixed = schedule.iso_z(NOW + timedelta(minutes=90))
    later = NOW + timedelta(days=9)
    resolved = schedule.resolve(
        mode=schedule.ABSOLUTE, offset_minutes=None, scheduled_at=fixed, now=later
    )
    assert not schedule.validate(resolved, now=later).ok


def test_naive_timestamps_are_read_as_utc():
    """Guessing local time here would silently shift a post by hours."""
    parsed = schedule.parse_iso("2026-08-20T12:00:00")
    assert parsed == NOW


def test_iso_round_trip_keeps_the_instant():
    assert schedule.parse_iso(schedule.iso_z(NOW)) == NOW


def test_spread_keeps_the_interval_and_never_starts_too_soon():
    times = schedule.spread(3, NOW, 23, now=NOW)
    assert times[0] == schedule.earliest_publish(NOW)
    assert times[1] - times[0] == timedelta(minutes=23)
    assert all(schedule.validate(when, now=NOW).ok for when in times)


def test_the_minimum_matches_civitai():
    assert config.POST_MINIMUM_SCHEDULE_MINUTES == 60


# --- confirmed time vs. local plan -------------------------------------------


def test_a_confirmed_remote_time_wins_over_the_local_plan():
    """The bug this prevents: a relative plan re-resolved against the current
    clock slides forward every time it is displayed, so a post already fixed on
    CivitAI was shown later and later - and out of order next to its neighbours.
    """
    post = {
        "remote_published_at": "2026-08-20T23:55:56.000Z",
        "schedule_mode": "relative",
        "schedule_offset_minutes": 120,
    }
    later = NOW + timedelta(days=3)
    assert schedule.effective_publish_at(post, now=NOW) == schedule.parse_iso(
        post["remote_published_at"]
    )
    # and it stays put however much later you look
    assert schedule.effective_publish_at(post, now=later) == schedule.effective_publish_at(
        post, now=NOW
    )


def test_an_unpushed_post_still_uses_its_local_plan():
    post = {
        "remote_published_at": None,
        "schedule_mode": "relative",
        "schedule_offset_minutes": 120,
    }
    assert schedule.effective_publish_at(post, now=NOW) == NOW + timedelta(minutes=120)


def test_pinned_says_whether_the_time_is_fixed_on_civitai():
    assert schedule.is_pinned({"remote_published_at": "2026-08-20T23:55:56.000Z"})
    assert not schedule.is_pinned({"remote_published_at": None})
    assert not schedule.is_pinned({})


def test_the_calendar_orders_pushed_posts_by_their_real_time(client, scanned):
    """The reported symptom: a post fixed earlier on CivitAI appeared after one
    scheduled later, because only the later one was being read correctly."""
    from backend.store import posts as post_store

    early = client.post(
        "/api/posts",
        json={"image_ids": scanned, "title": "Earlier", "schedule_offset_minutes": 400},
    ).json()["id"]
    late = client.post(
        "/api/posts", json={"image_ids": scanned, "title": "Later", "schedule_offset_minutes": 90}
    ).json()["id"]

    # both already on CivitAI, with times that contradict the local offsets
    post_store.set_fields(early, remote_published_at="2026-12-01T01:55:56.000Z")
    post_store.set_fields(late, remote_published_at="2026-12-01T05:30:00.000Z")

    entries = client.get("/api/schedule/calendar").json()["entries"]
    titles = [e["title"] for e in entries if e["title"] in ("Earlier", "Later")]
    assert titles == ["Earlier", "Later"], "order follows the confirmed time"

    pinned = {e["title"]: e for e in entries if e["title"] in ("Earlier", "Later")}
    assert pinned["Earlier"]["publish_at"].startswith("2026-12-01T01:55")
    assert pinned["Earlier"]["pinned"] is True
    assert pinned["Earlier"]["offset_label"] == "", "no misleading 'in X h' any more"


def test_the_calendar_answers_for_a_window(client, scanned):
    """Without one it returned every post that ever had a time, which only grows."""
    from backend.store import posts as post_store

    near = post_store.create(title="Soon", publish_mode="schedule", schedule_mode="absolute")
    post_store.set_fields(near, scheduled_at="2026-12-01T12:00:00Z")
    far = post_store.create(title="Much later", publish_mode="schedule", schedule_mode="absolute")
    post_store.set_fields(far, scheduled_at="2027-06-01T12:00:00Z")
    everything = client.get("/api/schedule/calendar").json()
    titles = {entry["title"] for entry in everything["entries"]}
    assert {"Soon", "Much later"} <= titles

    windowed = client.get(
        "/api/schedule/calendar",
        params={"start": "2026-11-01T00:00:00Z", "end": "2026-12-31T00:00:00Z"},
    ).json()
    windowed_titles = {entry["title"] for entry in windowed["entries"]}
    assert "Soon" in windowed_titles
    assert "Much later" not in windowed_titles


def test_the_calendar_window_includes_candidates_past_the_post_page(client, monkeypatch):
    """Relative plans have no stored instant, so SQL must not window them out."""
    from backend.api import schedule as schedule_api
    from backend.store import posts as post_store

    monkeypatch.setattr(schedule_api.sched, "utcnow", lambda: NOW)
    for index in range(598):
        outside = post_store.create(
            title=f"Outside {index}", publish_mode="schedule", schedule_mode="absolute"
        )
        post_store.set_fields(outside, scheduled_at="2026-01-01T12:00:00Z")
    absolute = post_store.create(
        title="Late absolute", publish_mode="schedule", schedule_mode="absolute"
    )
    post_store.set_fields(absolute, scheduled_at="2026-08-20T14:30:00Z")
    post_store.create(
        title="Late relative",
        publish_mode="schedule",
        schedule_mode="relative",
        schedule_offset_minutes=120,
    )

    calendar = client.get(
        "/api/schedule/calendar",
        params={"start": "2026-08-20T13:00:00Z", "end": "2026-08-20T15:00:00Z"},
    ).json()

    assert {entry["title"] for entry in calendar["entries"]} == {
        "Late absolute",
        "Late relative",
    }
    assert calendar["per_day"] == {"2026-08-20": 2}


def test_a_confirmed_time_on_the_window_edge_survives_the_prefilter(client, monkeypatch):
    """CivitAI sends `publishedAt` with and without milliseconds.

    The stored string is compared in SQL, the resolved one in Python. Without a
    date-level prefilter `...T15:00:00Z` sorts after `...T15:00:00.000Z`, so a
    post published on the window's last second would vanish from the calendar.
    """
    from backend.api import schedule as schedule_api
    from backend.store import posts as post_store

    monkeypatch.setattr(schedule_api.sched, "utcnow", lambda: NOW)
    edge = post_store.create(title="On the edge", publish_mode="schedule")
    post_store.set_fields(edge, remote_published_at="2026-08-20T15:00:00Z")

    calendar = client.get(
        "/api/schedule/calendar",
        params={"start": "2026-08-20T13:00:00.000Z", "end": "2026-08-20T15:00:00.000Z"},
    ).json()

    assert [entry["title"] for entry in calendar["entries"]] == ["On the edge"]

