"""Tests for background research tools (real threads; injected research fn)."""
from __future__ import annotations

import threading
import time

from nova.harness.jobs import JobManager
from nova.tools.agents import GetResearchResultTool, StartResearchTool


def _wait_done(jm, job_id, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if jm.get(job_id).status != "running":
            return True
        time.sleep(0.02)
    return False


def test_start_research_returns_job_id_immediately():
    jm = JobManager()
    tool = StartResearchTool(jm, lambda query: {"status": "success", "answer": "A: " + query})
    out = tool.execute(topic="quantum tires")
    assert out["status"] == "started"
    assert out["job_id"]
    assert "announce" in out["note"].lower() or "ready" in out["note"].lower()


def test_get_research_result_returns_finished_answer():
    jm = JobManager()
    start = StartResearchTool(jm, lambda query: {"status": "success", "answer": "42"})
    job_id = start.execute(topic="x")["job_id"]
    assert _wait_done(jm, job_id)
    result = GetResearchResultTool(jm).execute(job_id=job_id)
    assert result["status"] == "success"
    assert result["answer"] == "42"


def test_get_research_result_no_id_returns_latest_and_running_status():
    jm = JobManager()
    release = threading.Event()

    def slow(query):
        release.wait(5.0)
        return {"status": "success", "answer": "later"}

    start = StartResearchTool(jm, slow)
    job_id = start.execute(topic="slow")["job_id"]
    running = GetResearchResultTool(jm).execute()
    assert running["status"] == "running"
    assert running["job_id"] == job_id
    release.set()
    assert _wait_done(jm, job_id)
    assert GetResearchResultTool(jm).execute()["answer"] == "later"


def test_get_research_result_unknown_id():
    out = GetResearchResultTool(JobManager()).execute(job_id="nope")
    assert out["status"] == "not_found"


def test_schemas_well_formed():
    jm = JobManager()
    for tool in (StartResearchTool(jm, lambda query: {}), GetResearchResultTool(jm)):
        ft = tool.to_function_tool()
        assert ft["type"] == "function"
        assert ft["name"] and ft["description"]
        assert ft["parameters"]["type"] == "object"
