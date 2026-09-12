"""Background-job exclusivity and process-shutdown safety."""

from __future__ import annotations

import logging
import threading
import time

import pytest
from fastapi.testclient import TestClient

from backend import jobs


def _wait_until_finished(
    job: jobs.Job, timeout: float = 1.0, expected: str = "done"
) -> None:
    deadline = time.monotonic() + timeout
    while job.status in ("starting", "running") and time.monotonic() < deadline:
        time.sleep(0.005)
    assert job.status == expected


def test_existing_global_reservation_blocks_a_new_scoped_job():
    with (
        jobs.reserve("restore", exclusive="*") as restore,
        pytest.raises(jobs.AlreadyRunning) as raised,
    ):
        jobs.start("scan", lambda job: None, exclusive="scan")

    assert raised.value.job is restore


def test_existing_scoped_job_blocks_a_new_global_reservation():
    started = threading.Event()
    release = threading.Event()

    def scan(job: jobs.Job) -> None:
        started.set()
        release.wait(1.0)

    scan_job = jobs.start("scan", scan, exclusive="scan")
    assert started.wait(1.0)
    try:
        with pytest.raises(jobs.AlreadyRunning) as raised, jobs.reserve(
            "restore", exclusive="*"
        ):
            pass
        assert raised.value.job is scan_job
    finally:
        release.set()
        _wait_until_finished(scan_job)


@pytest.mark.parametrize("kind", sorted(jobs.FILESYSTEM_MUTATING_KINDS))
def test_shutdown_waits_for_a_filesystem_mutating_job(kind):
    from backend.main import create_app

    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def mutate_files(job: jobs.Job) -> None:
        started.set()
        release.wait(1.0)
        finished.set()

    timer = threading.Timer(0.1, release.set)
    with TestClient(create_app()):
        jobs.start(kind, mutate_files, exclusive=kind)
        assert started.wait(1.0)
        timer.start()

    assert finished.is_set()
    timer.join()


def test_shutdown_logs_a_filesystem_job_that_outlives_the_timeout(
    monkeypatch, caplog
):
    from backend import main

    started = threading.Event()
    release = threading.Event()

    def archive(job: jobs.Job) -> None:
        started.set()
        release.wait(1.0)

    monkeypatch.setattr(main, "_FILESYSTEM_JOB_SHUTDOWN_TIMEOUT_SECONDS", 0.0)
    job = None
    try:
        with caplog.at_level(
            logging.WARNING, logger=main.__name__
        ), TestClient(main.create_app()):
            job = jobs.start("archive", archive, exclusive="archive")
            assert started.wait(1.0)

        assert f"{job.id} (archive)" in caplog.text
        assert "Shutdown timed out" in caplog.text
    finally:
        release.set()
        if job is not None:
            _wait_until_finished(job)


def test_the_active_job_route_is_not_read_as_a_job_id(client):
    response = client.get("/api/jobs/active")

    assert response.status_code == 200
    assert response.json()["items"] == [], "nothing is running at the baseline"


def test_jobs_that_finish_between_polls_are_reported_once_with_results_and_errors(client):
    baseline = client.get("/api/jobs/active").json()["cursor"]

    def succeed(job: jobs.Job) -> None:
        job.result = {"changed": 1}

    def fail(job: jobs.Job) -> None:
        job.status = "error"
        job.error = "the short check failed"

    succeeded = jobs.start("short-success", succeed)
    failed = jobs.start("short-error", fail)
    _wait_until_finished(succeeded)
    _wait_until_finished(failed, expected="error")

    first = client.get("/api/jobs/active", params={"after": baseline}).json()
    assert [item["id"] for item in first["items"]] == [succeeded.id, failed.id]
    assert first["items"][0]["result"] == {"changed": 1}
    assert first["items"][1]["error"] == "the short check failed"

    second = client.get("/api/jobs/active", params={"after": first["cursor"]}).json()
    assert second["items"] == [], "the cursor does not replay either finished job"
