"""Cloud LLM failover: a rate-limited or unreachable provider never ends a voice turn."""
import pytest

from nova_hailo.backends.failover_llm import FailoverLLM, build_failover, retry_reason
from nova_hailo.metrics import InferenceMetrics


class ConnectTimeout(Exception):
    pass


class FakeLLM:
    def __init__(self, provider, *, fail=None, tokens=("ok",), model="openai/gpt-oss-120b"):
        self.provider, self.fail, self.tokens, self.model = provider, fail, tokens, model
        self.hef_path = f"{provider}:{model}"
        self.calls = 0
        self.cleared = self.released = 0
        self.last_tool_calls = [{"name": f"{provider}_tool"}]

    def generate(self, messages, max_tokens=None, token_callback=None, **kw):
        self.calls += 1
        if self.fail == "midstream":
            token_callback and token_callback("Hel")
            raise ConnectTimeout("stream cut")
        if self.fail:
            raise self.fail
        for t in self.tokens:
            token_callback and token_callback(t)
        m = InferenceMetrics()
        m.device = self.provider
        return "".join(self.tokens), m

    def clear(self):
        self.cleared += 1

    def release(self):
        self.released += 1


class Clock:
    t = 100.0

    def __call__(self):
        return self.t


def http(status):
    return RuntimeError(f"groq HTTP {status}: {{\"error\": \"x\"}}")


def test_retry_reasons():
    assert retry_reason(http(429)) == "429"
    assert retry_reason(http(503)) == "503"
    assert retry_reason(ConnectTimeout("x")) == "timeout"
    assert retry_reason(type("ConnectError", (Exception,), {})("x")) == "connection"
    assert retry_reason(http(400)) is None and retry_reason(ValueError("x")) is None


def test_a_rate_limited_turn_is_answered_by_the_other_provider(capsys):
    groq, orr = FakeLLM("groq", fail=http(429)), FakeLLM("openrouter")
    llm = FailoverLLM(groq, orr)
    spoken = []
    text, m = llm.generate([{"role": "user", "content": "hi"}], token_callback=spoken.append)
    assert text == "ok" and spoken == ["ok"]
    assert m.device == "openrouter" and m.failover == "groq→openrouter (429)"
    assert "[llm] failover groq→openrouter (429)" in capsys.readouterr().out
    assert llm.last_tool_calls == [{"name": "openrouter_tool"}]     # the answering client's
    assert llm.hef_path == "openrouter:openai/gpt-oss-120b"
    assert m.to_dict()["failover"] == "groq→openrouter (429)"


def test_the_failed_provider_is_skipped_for_a_minute():
    clock = Clock()
    groq, orr = FakeLLM("groq", fail=http(429)), FakeLLM("openrouter")
    llm = FailoverLLM(groq, orr, hold_s=60, clock=clock)
    llm.generate([])
    groq.fail = None
    clock.t += 30
    _, m = llm.generate([])
    assert groq.calls == 1 and orr.calls == 2 and m.failover is None
    clock.t += 31
    _, m = llm.generate([])
    assert groq.calls == 2 and m.device == "groq"


def test_errors_that_another_provider_cannot_fix_are_raised():
    groq, orr = FakeLLM("groq", fail=http(400)), FakeLLM("openrouter")
    with pytest.raises(RuntimeError):
        FailoverLLM(groq, orr).generate([])
    assert orr.calls == 0


def test_a_reply_that_already_started_speaking_is_not_repeated():
    groq, orr = FakeLLM("groq", fail="midstream"), FakeLLM("openrouter")
    spoken = []
    with pytest.raises(ConnectTimeout):
        FailoverLLM(groq, orr).generate([], token_callback=spoken.append)
    assert spoken == ["Hel"] and orr.calls == 0


def test_both_down_raises_the_last_error():
    groq, orr = FakeLLM("groq", fail=http(429)), FakeLLM("openrouter", fail=http(502))
    with pytest.raises(RuntimeError, match="502"):
        FailoverLLM(groq, orr).generate([])


def test_clear_and_release_reach_both_clients():
    groq, orr = FakeLLM("groq"), FakeLLM("openrouter")
    llm = FailoverLLM(groq, orr)
    llm.clear()
    llm.release()
    assert (groq.cleared, orr.cleared, groq.released, orr.released) == (1, 1, 1, 1)


def test_build_pairs_groq_and_openrouter_when_the_other_key_is_set():
    made = []

    def make(provider):
        made.append(provider)
        return FakeLLM(provider)

    primary = FakeLLM("groq")
    env = {"OPENROUTER_API_KEY": "k"}
    llm = build_failover("groq", primary, make, env=env)
    assert isinstance(llm, FailoverLLM) and made == ["openrouter"]
    assert build_failover("groq", primary, make, env={}) is primary         # no other key
    assert build_failover("groq", primary, make, env=env, enabled=False) is primary
    assert build_failover("openrouter", primary, make, env={"GROQ_API_KEY": "g"}).secondary.provider == "groq"
    assert build_failover("python", primary, make, env=env) is primary      # local Hailo: no pair


def test_a_provider_without_a_key_gives_way_to_its_partner():
    from nova_hailo.backends.failover_llm import usable_provider

    assert usable_provider("openrouter", env={"GROQ_API_KEY": "g"}) == "groq"
    assert usable_provider("groq", env={"OPENROUTER_API_KEY": "k"}) == "openrouter"
    assert usable_provider("groq", env={"GROQ_API_KEY": "g", "OPENROUTER_API_KEY": "k"}) == "groq"
    assert usable_provider("groq", env={}) == "groq"            # neither: the turn says so itself
    assert usable_provider("cpp", env={}) == "cpp"              # local Hailo is not a cloud provider


def test_the_planner_uses_the_partner_when_its_provider_has_no_key(monkeypatch):
    from types import SimpleNamespace

    from nova_hailo.edge_harness.backend_agent import build_backend_agent

    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OR_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "g")
    made = {}

    def factory(**kw):
        made.update(kw)
        return SimpleNamespace(**kw)

    cfg = {("harness", "backend"): "openrouter"}
    gateway = SimpleNamespace(tasks=object(), cabin=object(), vehicle=object())
    agent = build_backend_agent(lambda s, k, default=None: cfg.get((s, k), default), gateway,
                                llm_factory=factory)
    assert agent is not None and made["provider"] == "groq"
