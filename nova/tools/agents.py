"""Agentic tools: background research jobs the model can start and poll.

``start_research`` returns immediately with a job id (the JobManager runs the
real research in a daemon thread); Nova announces completion proactively (the
UI polls /jobs/announcements). ``get_research_result`` lets the model fetch
the finished answer on demand ("what did the research find?").
"""
from __future__ import annotations

from typing import Any, Callable

from nova.harness.jobs import JobManager
from nova.tools.base import NovaTool


class StartResearchTool(NovaTool):
    name = "start_research"
    description = (
        "Start a deep background research job on a topic. Returns immediately with a job id; "
        "the results are announced when ready (do NOT wait or poll — tell the user you'll "
        "let them know). Use for open-ended research questions that need more than a quick "
        "web search."
    )
    parameters = {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "What to research, as a full question."},
        },
        "required": ["topic"],
    }

    def __init__(self, jobs: JobManager, research_fn: Callable[..., dict[str, Any]]):
        self._jobs = jobs
        self._research_fn = research_fn

    def execute(self, topic: str) -> dict[str, Any]:
        job_id = self._jobs.start("research", self._research_fn, query=topic)
        return {
            "status": "started",
            "job_id": job_id,
            "note": "Research started in the background; the result will be announced when ready.",
        }


class GetResearchResultTool(NovaTool):
    name = "get_research_result"
    description = (
        "Fetch the result of a background research job. Omit job_id to get the most recent "
        "research job. Returns the answer if finished, or its current status."
    )
    parameters = {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "Job id from start_research. Optional."},
        },
        "required": [],
    }

    def __init__(self, jobs: JobManager):
        self._jobs = jobs

    def execute(self, job_id: str = "") -> dict[str, Any]:
        if job_id:
            job = self._jobs.get(job_id)
        else:
            research = [j for j in self._jobs.list() if j.kind == "research"]
            job = max(research, key=lambda j: j.created_at) if research else None
        if job is None:
            return {"status": "not_found", "job_id": job_id or None}
        if job.status == "running":
            return {"status": "running", "job_id": job.id}
        if job.status == "failed":
            return {"status": "failed", "job_id": job.id, "error": job.error}
        return {**(job.result or {}), "job_id": job.id}
