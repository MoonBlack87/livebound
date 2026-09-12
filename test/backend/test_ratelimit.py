"""The local post count must never masquerade as CivitAI's unknown ceiling."""

from __future__ import annotations


def test_an_unknown_limit_reports_only_the_local_count_and_no_issue(monkeypatch):
    from backend.api import schedule as schedule_api
    from backend.civitai import ratelimit
    from backend.posts import validation

    monkeypatch.setattr(ratelimit, "_created_since", lambda hours: 4)

    assert ratelimit.daily_limit() is None
    assert ratelimit.budget() == {"limit": None, "used": 4}
    assert validation._check_budget() == []
    assert schedule_api.calendar()["daily_limit"] is None


def test_an_override_restores_the_ceiling_remaining_count_and_issue(monkeypatch):
    from backend import db
    from backend.api import schedule as schedule_api
    from backend.civitai import ratelimit
    from backend.posts import validation

    db.set_setting("rate_limit_posts_per_day", "3")
    monkeypatch.setattr(ratelimit, "_created_since", lambda hours: 3)

    assert ratelimit.budget() == {"limit": 3, "used": 3, "remaining": 0}
    [issue] = validation._check_budget()
    assert (issue.code, issue.params) == ("rate_limit_daily", {"used": 3, "limit": 3})
    assert schedule_api.calendar()["daily_limit"] == 3
