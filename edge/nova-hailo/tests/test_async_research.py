"""deep_research must not block the turn; results land on a later idle tick."""
from __future__ import annotations

from nova_hailo.tools.research_jobs import STATUS_DONE, STATUS_SEARCHING


def test_drain_returns_only_finished_jobs():
    from nova_hailo.pipeline import PendingResearch

    pend = PendingResearch()
    pend.add("job-a")
    pend.add("job-b")
    statuses = {"job-a": STATUS_DONE, "job-b": STATUS_SEARCHING}

    def poll(jid):
        return {"status": statuses[jid], "speak": f"result for {jid}", "ok": True}

    out = pend.drain(poll)
    assert [o["job_id"] for o in out] == ["job-a"]
    assert pend.ids() == ["job-b"]          # unfinished job stays registered
    assert pend.drain(poll) == []           # finished job is never delivered twice


def test_drain_is_safe_when_empty():
    from nova_hailo.pipeline import PendingResearch

    assert PendingResearch().drain(lambda jid: {}) == []


def test_drain_survives_a_raising_poll():
    from nova_hailo.pipeline import PendingResearch

    pend = PendingResearch()
    pend.add("job-a")

    def poll(jid):
        raise RuntimeError("network down")

    assert pend.drain(poll) == []
    assert pend.ids() == ["job-a"]          # stays queued for the next tick


def test_drain_carries_the_originating_question():
    """The delayed delivery must record the real question with the answer,
    not an empty one -- add() takes an optional user_text that drain()
    hands back so history isn't paired with a blank turn."""
    from nova_hailo.pipeline import PendingResearch

    pend = PendingResearch()
    pend.add("job-a", user_text="what's the weather in Tokyo tomorrow")

    def poll(jid):
        return {"status": STATUS_DONE, "speak": "Sunny and mild.", "ok": True}

    out = pend.drain(poll)
    assert out == [
        {
            "job_id": "job-a",
            "speak": "Sunny and mild.",
            "ok": True,
            "user_text": "what's the weather in Tokyo tomorrow",
        }
    ]


def test_add_without_user_text_still_works():
    """The plan's stated call shape, add(job_id) with no user_text, must
    keep working -- drain() just hands back an empty string for it."""
    from nova_hailo.pipeline import PendingResearch

    pend = PendingResearch()
    pend.add("job-a")

    def poll(jid):
        return {"status": STATUS_DONE, "speak": "done", "ok": True}

    out = pend.drain(poll)
    assert out[0]["user_text"] == ""
