"""Real-thread tests for the background JobManager (no mocking)."""
from __future__ import annotations

import threading
import time

from nova.harness.jobs import JobManager


def _wait_for(predicate, timeout=5.0, interval=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_job_runs_and_completes_with_result():
    jm = JobManager()
    job_id = jm.start("test", lambda topic: {"status": "success", "answer": topic.upper()},
                      topic="hello")
    assert isinstance(job_id, str) and job_id
    assert _wait_for(lambda: jm.get(job_id).status == "done")
    job = jm.get(job_id)
    assert job.result == {"status": "success", "answer": "HELLO"}
    assert job.error == ""
    assert job.finished_at is not None


def test_job_failure_is_recorded_not_raised():
    def boom():
        raise ValueError("boom")
    jm = JobManager()
    job_id = jm.start("test", boom)
    assert _wait_for(lambda: jm.get(job_id).status == "failed")
    job = jm.get(job_id)
    assert "boom" in job.error
    assert job.result is None


def test_pending_announcements_delivered_exactly_once():
    jm = JobManager()
    job_id = jm.start("test", lambda: {"ok": True})
    assert _wait_for(lambda: jm.get(job_id).status == "done")
    first = jm.pending_announcements()
    assert [j.id for j in first] == [job_id]
    assert jm.pending_announcements() == []


def test_running_job_is_not_announced():
    release = threading.Event()

    def slow():
        release.wait(5.0)
        return {"ok": True}

    jm = JobManager()
    job_id = jm.start("test", slow)
    assert jm.pending_announcements() == []
    release.set()
    assert _wait_for(lambda: jm.get(job_id).status == "done")
    assert [j.id for j in jm.pending_announcements()] == [job_id]


def test_unknown_job_id_returns_none_and_list_lists_all():
    jm = JobManager()
    assert jm.get("nope") is None
    a = jm.start("x", lambda: {})
    b = jm.start("y", lambda: {})
    assert _wait_for(lambda: all(jm.get(i).status == "done" for i in (a, b)))
    assert {j.id for j in jm.list()} == {a, b}
