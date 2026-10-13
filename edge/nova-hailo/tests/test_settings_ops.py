"""settings_ops: transport-free nova.settings logic shared by /v1/realtime and LiveKit."""
import asyncio

from nova_hailo.web import settings_ops


class _Pipe:
    def __init__(self, *, fail=None):
        self.fail = fail
        self.applied = None

    def llm_settings_snapshot(self):
        return {"mode": "local", "local_hef": "q.hef"}

    def apply_llm_settings(self, *, mode=None, local_hef=None, or_model=None):
        if self.fail:
            raise RuntimeError(self.fail)
        self.applied = {"mode": mode, "local_hef": local_hef, "or_model": or_model}
        return {"mode": mode, "or_model": or_model}


def test_llm_snapshot_reads_the_pipeline():
    assert settings_ops.llm_snapshot(_Pipe()) == {"mode": "local", "local_hef": "q.hef"}


def test_llm_snapshot_never_raises():
    class Broken:
        def llm_settings_snapshot(self):
            raise RuntimeError("boom")

    assert settings_ops.llm_snapshot(Broken()) == {}
    assert settings_ops.llm_snapshot(object()) == {}


def test_settings_body_accepts_nested_or_flat_messages():
    assert settings_ops.settings_body({"type": "x", "settings": {"mode": "local"}}) == {"mode": "local"}
    flat = {"type": "x", "mode": "openrouter"}
    assert settings_ops.settings_body(flat) is flat


def test_changes_llm_only_when_mode_or_cloud_model_given():
    assert settings_ops.changes_llm({"mode": "local"})
    assert settings_ops.changes_llm({"or_model": "groq/x"})
    assert not settings_ops.changes_llm({"gate_min_rms": 0.01})


def test_apply_llm_settings_success_returns_snapshot_and_ready():
    pipe = _Pipe()
    snap, status = asyncio.run(
        settings_ops.apply_llm_settings(pipe, {"mode": "openrouter", "or_model": "m"})
    )
    assert pipe.applied == {"mode": "openrouter", "local_hef": None, "or_model": "m"}
    assert snap == {"mode": "openrouter", "or_model": "m"}
    assert status == {"type": "nova.llm_status", "status": "ready"}


def test_apply_llm_settings_failure_reports_error_without_snapshot():
    snap, status = asyncio.run(settings_ops.apply_llm_settings(_Pipe(fail="no hef"), {"mode": "local"}))
    assert snap is None
    assert status == {"type": "nova.llm_status", "status": "error", "error": "no hef"}


def test_apply_llm_settings_unsupported_pipeline():
    snap, status = asyncio.run(settings_ops.apply_llm_settings(object(), {"mode": "local"}))
    assert snap is None
    assert status["status"] == "error" and "not supported" in status["error"]
