"""Offline OEM demo unit tests — contract, FSM, history, tool router.

Callers: scripts/verify_oem_gates.sh, pytest.
"""
from __future__ import annotations

from pathlib import Path

from nova_hailo.context_history import ConversationHistory
from nova_hailo.response_contract import first_complete_clause, validate_spoken_unit
from nova_hailo.session_fsm import SessionFSM, SessionState, TurnTerminal
from nova_hailo.tools.oem_tools import OemToolGateway


def test_response_contract_rejects_control_tokens():
    bad = validate_spoken_unit("<|im_start|>assistant hello")
    assert bad["ok"] is False
    assert bad["fallback"] is True
    assert "control_token" in bad["failures"] or "role_leak" in bad["failures"]


def test_response_contract_rejects_unsafe_claims():
    bad = validate_spoken_unit("Payment sent to your landlord.")
    assert bad["ok"] is False
    assert "unsafe_action_claim" in bad["failures"]


def test_response_contract_accepts_short_speak():
    ok = validate_spoken_unit("You have three meetings this afternoon.")
    assert ok["ok"] is True
    assert ok["speak"].startswith("You have")


def test_response_contract_trims_numbered_list_cut_off_at_marker():
    """A numbered list cut off right after its marker ("... 2.") ends in
    "." but isn't a complete sentence -- must trim back to the real
    sentence end, not stop at the list-marker period."""
    out = validate_spoken_unit(
        "The ongoing news of the Iran War is as follows: 1. Iran continues "
        "Hormuz talks with Oman and US signals optimism on a deal. 2."
    )
    assert out["ok"] is True
    assert out["speak"].endswith("on a deal.")
    assert "trimmed_incomplete_sentence" in out["failures"]


def test_response_contract_trims_decimal_cut_off_mid_number():
    """Same bug, decimal variant: "...54°F (12." must not be spoken as-is,
    and the fallback period must not double up into "12..""."""
    out = validate_spoken_unit(
        "The weather in Delhi is cloudy with a low temperature of 54°F (12."
    )
    assert out["ok"] is True
    assert ".." not in out["speak"]
    assert out["speak"].endswith(".")


def test_response_contract_does_not_over_trim_a_genuinely_complete_list():
    ok = validate_spoken_unit("1. First item is here. 2. Second item is also here.")
    assert ok["ok"] is True
    assert ok["speak"] == "1. First item is here. 2. Second item is also here."
    assert "trimmed_incomplete_sentence" not in ok["failures"]


def test_first_complete_clause():
    clause, rest = first_complete_clause("Hello there. More words", min_chars=5)
    assert clause == "Hello there."
    assert rest.strip().startswith("More")


def test_first_complete_clause_ignores_decimal_period():
    clause, rest = first_complete_clause(
        "The value is 3.14 meters long.", min_chars=5
    )
    assert clause == "The value is 3.14 meters long."
    assert rest == ""


def test_first_complete_clause_ignores_abbreviation_period():
    clause, rest = first_complete_clause(
        "Please ask Dr. Smith about it.", min_chars=5
    )
    assert clause == "Please ask Dr. Smith about it."
    assert rest == ""


def test_first_complete_clause_decimal_not_yet_fully_streamed():
    # Buffer ends right at the decimal point; more digits haven't arrived
    # yet. Must not split here -- correct behavior is to wait for more input.
    clause, rest = first_complete_clause("The value is 3.", min_chars=5)
    assert clause is None
    assert rest == "The value is 3."


def test_history_bounded_to_two_turns():
    h = ConversationHistory(max_turns=2)
    h.add("a", "A")
    h.add("b", "B")
    h.add("c", "C")
    assert len(h) == 2
    msgs = h.build_messages("sys", "now")
    assert msgs[0]["role"] == "system"
    users = [m["content"] for m in msgs if m["role"] == "user"]
    assert "b" in users[0]
    assert users[-1].startswith("now")


def test_history_rejects_entity_bleed_from_previous_reply():
    """Measured live: a wrong 'NVIDIA's stock price...' answer mutated into
    'NVIDIA's vehicle status...', '...windows...', '...lights...' across four
    unrelated turns once it entered history. None of those are refusals, so
    the refusal filter never caught them -- must reject storing a reply that
    reuses an entity from the prior reply the current question never asked
    about."""
    h = ConversationHistory(max_turns=3)
    h.add(
        "Could you check again for the NVIDIA stock price?",
        "NVIDIA's stock price as of today is $36 per share.",
    )
    assert len(h) == 1
    h.add(
        "Hey check to check my vehicle status.",
        "NVIDIA's vehicle status is currently fine.",
    )
    assert len(h) == 1  # bled reply rejected, not stored
    h.add(
        "What is the weather in Bangalore?",
        "It's partly cloudy in Bangalore, around 28 degrees.",
    )
    assert len(h) == 2  # unrelated, legitimate reply still stores normally


def test_fsm_on_change_emits_snapshot():
    seen: list[str] = []
    fsm = SessionFSM(on_change=lambda snap: seen.append(str(snap.get("state"))))
    fsm.arm()
    fsm.begin_listen()
    assert "ARMED" in seen
    assert "LISTENING" in seen


def test_fsm_generation_invalidates_on_interrupt():
    fsm = SessionFSM()
    fsm.arm()
    ctx = fsm.begin_turn()
    gid = ctx.generation_id
    assert fsm.set_thinking(gid)
    fsm.interrupt("barge")
    assert fsm.is_current(gid) is False
    assert fsm.state == SessionState.INTERRUPTING


def test_fsm_complete_only_current():
    fsm = SessionFSM()
    ctx = fsm.begin_turn()
    assert fsm.complete(ctx.generation_id, TurnTerminal.COMPLETED)
    assert fsm.complete(ctx.generation_id, TurnTerminal.COMPLETED) is False


def test_websearch_profile_routes_search_and_research():
    gw = OemToolGateway(enabled=["web_search", "deep_research"])
    assert gw.route("Search the web for hailo") == "web_search"
    assert gw.route("Research the latest Hailo GenAI docs") == "deep_research"
    assert gw.route("Dig into quantum computing") == "deep_research"
    assert gw.route("What is your name?") == "identity"
    # Workspace stays off in this profile.
    assert gw.route("What's on my calendar?") == "capability_unavailable"
    # Live demo phrasing must hit web_search (not chat+soul refusal).
    assert gw.route("Hey no, what is the current news today?") == "web_search"
    assert gw.route("what is the current news today in Delhi?") == "web_search"
    assert gw.route("bring me the latest news in India") == "web_search"
    assert gw.route("tell me the current weather in Bangalore") == "web_search"


def test_deep_research_fail_closed_without_tavily(monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    gw = OemToolGateway(enabled=["web_search", "deep_research"])
    out = gw.execute("deep_research", query="research hailo")
    assert out["ok"] is False
    assert out["status"] == "unavailable"


def test_research_job_fsm_without_network(monkeypatch):
    """Status transitions searching → generating → done with a mocked worker."""
    import time

    from nova_hailo.tools import research_jobs as rj
    from nova_hailo.tools import search_mcp as sm

    store = rj.ResearchJobStore()

    def fake_worker(job: rj.ResearchJob) -> None:
        store.update(job.job_id, status=rj.STATUS_SEARCHING)
        store.update(job.job_id, status=rj.STATUS_GENERATING)
        store.update(
            job.job_id,
            status=rj.STATUS_DONE,
            speak="Hailo is an AI accelerator company.",
            evidence="Hailo makes edge NPUs.",
        )

    monkeypatch.setenv("TAVILY_API_KEY", "test-key")
    job, reason = store.try_begin("hailo npu")
    assert reason is None and job is not None
    store.start_worker(job, fake_worker)
    deadline = time.monotonic() + 2.0
    status = None
    while time.monotonic() < deadline:
        st = sm.research_status(job.job_id, store=store)
        status = st.get("status")
        if status in {rj.STATUS_DONE, rj.STATUS_FAILED}:
            break
        time.sleep(0.05)
    assert status == rj.STATUS_DONE
    assert "Hailo" in (sm.research_status(job.job_id, store=store).get("speak") or "")


def test_search_cleaner_caps_evidence():
    from nova_hailo.tools.search_clean import SearchResultCleaner

    cleaner = SearchResultCleaner(max_chars=200, max_results=2)
    hits = [
        {"title": f"Title {i}", "snippet": ("word " * 80), "source": "ex.com"}
        for i in range(5)
    ]
    block = cleaner.evidence_block(hits)
    assert len(block) <= 220
    assert block.count("\n") <= 2


def test_literal_number_ignores_later_results():
    """Measured live: the same stock query returned $369, then $218.99, then
    $36 across a few turns -- literal_number() was grabbing whichever number
    appeared first in the *whole* multi-result block, not necessarily from
    the top-ranked result. Must only look at the first result line."""
    from nova_hailo.tools.search_clean import literal_number

    evidence = (
        "1. AMZN: Amazon shares traded near $185.40 in late trading.\n"
        "2. Unrelated: some other page mentions $999.99 in a different context."
    )
    assert literal_number(evidence) == "$185.40"


def test_exa_body_prefers_summary_then_highlight():
    from nova_hailo.tools.search_mcp import _exa_body

    news = {
        "summary": "Iran and the US signaled progress on a Hormuz deal.",
        "highlights": ["Talks continued in Oman on Sunday."],
        "text": "Published 12 Aug 2026. 4 min read. Skip to content. Tehran (Reuters) - ...",
    }
    assert _exa_body(news, "latest news") == "Iran and the US signaled progress on a Hormuz deal."
    no_sum = {**news, "summary": ""}
    assert _exa_body(no_sum, "latest news") == "Talks continued in Oman on Sunday."
    stock = {
        "summary": "Amazon traded mixed.",
        "highlights": ["Shares moved after hours."],
        "text": "Amazon AMZN last price $185.40 in late trading.",
    }
    assert "$185.40" in _exa_body(stock, "stock price of Amazon today")


def test_speak_fallback_skips_page_chrome():
    from nova_hailo.tools.search_clean import SearchResultCleaner

    cleaner = SearchResultCleaner()
    hits = [
        {
            "title": "Wire",
            "snippet": "Published 12 August 2026. 4 min read. Tehran officials met Oman mediators.",
        },
        {
            "title": "Follow",
            "snippet": "The talks produced a draft ceasefire framework overnight.",
        },
    ]
    spoken = cleaner.speak_fallback(hits)
    assert "Published" not in spoken
    assert "min read" not in spoken
    assert "Tehran" in spoken or "ceasefire" in spoken


def test_answer_from_hits_tags_needs_summary():
    from nova_hailo.tools.search_mcp import answer_from_hits

    hits = [
        {
            "title": "Bengaluru weather",
            "snippet": "Expect partly cloudy skies with highs around 28 degrees Celsius today.",
            "source": "example.com",
        }
    ]
    out = answer_from_hits("current weather in Bangalore", hits)
    assert out["ok"] is True
    assert out["result"]["needs_summary"] is True
    assert out["result"]["numeric"] is False
    assert out["result"]["evidence"]


def test_answer_from_hits_numeric_bypass_no_summary():
    from nova_hailo.tools.search_mcp import answer_from_hits

    hits = [
        {
            "title": "AMZN",
            "snippet": "Amazon shares traded near $185.40 in late trading on Monday.",
            "source": "example.com",
        }
    ]
    out = answer_from_hits("stock price of Amazon today", hits)
    assert out["ok"] is True
    assert out["result"]["numeric"] is True
    assert out["result"]["needs_summary"] is False
    assert "185.40" in out["speak"] or "couldn't find" in out["speak"].lower()


def test_stock_search_query_rewrite_single_and_extended_tickers():
    """ticker map + stock-like rewrite (pure, no network)."""
    from nova_hailo.tools.search_mcp import _is_stock_query, _stock_search_query

    # Classic stock keyword path
    assert _is_stock_query("stock price of Amazon today")
    assert _stock_search_query("stock price of Amazon today") == "Amazon AMZN stock price USD"

    # Extended entities
    assert _stock_search_query("stock price of Palantir") == "Palantir PLTR stock price USD"
    assert _stock_search_query("what is the PLTR stock price") == "Palantir PLTR stock price USD"
    assert _stock_search_query("AMD stock price") == "AMD stock price USD"
    assert _stock_search_query("Intel share price") == "Intel INTC stock price USD"

    # Bitcoin / crypto: price ask without the word "stock"
    assert _is_stock_query("Bitcoin price")
    assert _is_stock_query("what is the price of bitcoin")
    btc_q = _stock_search_query("Bitcoin price")
    assert btc_q is not None
    assert "Bitcoin" in btc_q and "BTC" in btc_q and "price" in btc_q.lower()
    assert "stock" not in btc_q.lower()  # crypto rewrite prefers non-stock phrasing

    # price of <entity> without stock keyword
    assert _is_stock_query("price of NVIDIA")
    assert _stock_search_query("price of NVIDIA") == "NVIDIA NVDA stock price USD"

    # Non-stock stays None
    assert _stock_search_query("weather in Bangalore") is None
    assert not _is_stock_query("who is the current Prime Minister of the UK")


def test_stock_search_query_multi_ticker_rewrite():
    """Multi-entity queries must mention both tickers in the provider query."""
    from nova_hailo.tools.search_mcp import _stock_search_query

    q = _stock_search_query("stock price of NVIDIA and Tesla")
    assert q is not None
    assert "NVIDIA NVDA" in q
    assert "Tesla TSLA" in q
    assert "and" in q
    assert "stock price USD" in q

    q2 = _stock_search_query("stock price of palantir and bitcoin")
    assert q2 is not None
    assert "Palantir PLTR" in q2
    assert "Bitcoin BTC-USD" in q2


def test_multi_stock_speak_two_prices_and_partial():
    """dual-price speak string construction (pure)."""
    from nova_hailo.tools.search_mcp import _multi_stock_speak, answer_from_hits

    evidence_both = (
        "1. NVIDIA NVDA: NVIDIA stock price $875.50 trading at session highs. "
        "Tesla TSLA share price $248.30 after earnings."
    )
    speak = _multi_stock_speak("stock price of NVIDIA and Tesla", evidence_both)
    assert speak is not None
    assert speak.startswith("Search results say NVIDIA")
    assert "Tesla" in speak
    assert "$875.50" in speak
    assert "$248.30" in speak
    assert "and" in speak

    # No Tesla token near any $ amount — oil $72 must not become the second quote.
    evidence_one = (
        "1. Markets: NVIDIA closed at $875.50 on heavy volume. "
        "Oil futures settled at $72.10. Broader tech mixed; no auto quotes in this blurb."
    )
    speak_one = _multi_stock_speak("NVIDIA and Tesla stock", evidence_one)
    assert speak_one is not None
    assert "NVIDIA" in speak_one and "$875.50" in speak_one
    assert "couldn't find a reliable second price" in speak_one.lower()
    assert "$72.10" not in speak_one
    assert " and Tesla " not in speak_one

    # Via answer_from_hits end-to-end (still pure: synthetic hits, no HTTP)
    hits = [
        {
            "title": "NVDA and TSLA",
            "snippet": (
                "NVIDIA NVDA last price $875.50. Tesla TSLA trading at $248.30 per share today."
            ),
            "source": "example.com",
        }
    ]
    out = answer_from_hits("stock price of NVIDIA and Tesla", hits)
    assert out["ok"] is True
    assert out["result"]["numeric"] is True
    assert "NVIDIA" in out["speak"] and "Tesla" in out["speak"]
    assert "$875.50" in out["speak"] and "$248.30" in out["speak"]


def test_pick_stock_price_near_and_bitcoin_range():
    """Near-entity boost + crypto-sized quotes (pure)."""
    from nova_hailo.tools.search_clean import pick_stock_price

    body = (
        "Tesla rose $4.20 on the day. Tesla last price $248.30. "
        "NVIDIA trading at $875.50 after the split."
    )
    tesla = pick_stock_price(body, near=["Tesla", "TSLA"])
    assert tesla == "$248.30"
    nvidia = pick_stock_price(body, near=["NVIDIA", "NVDA"], exclude=[tesla])
    assert nvidia == "$875.50"

    btc_body = "Bitcoin BTC-USD price $95,432.10 in late trading, up $1,200 on the day."
    btc = pick_stock_price(btc_body, near=["Bitcoin", "BTC", "BTC-USD"])
    assert btc is not None
    assert "95,432" in btc or "95432" in btc.replace(",", "")


def test_summarize_evidence_mock_ok_and_fail():
    from nova_hailo.tools.search_summarizer import (
        build_summarizer_messages,
        is_summarizer_refusal,
        summarize_evidence,
    )

    msgs = build_summarizer_messages("weather in Bangalore", "1. Skies partly cloudy, 28C.")
    assert msgs[0]["role"] == "system"
    assert "Evidence" in msgs[1]["content"]
    assert is_summarizer_refusal(
        "I'm sorry, but as an AI language model, I don't have real-time weather."
    )
    # Pure persona-drift text (no Tier-B capability-refusal wording) is no
    # longer is_summarizer_refusal's job — it is caught upstream by
    # response_contract._PERSONA_DRIFT_RE inside summarize_evidence(), see the
    # end-to-end _RefuseLLM case below and tests/test_persona_drift.py.
    assert not is_summarizer_refusal("I'm not Nova anymore; I am now the vehicle called Nova.")
    assert not validate_spoken_unit(
        "I'm not Nova anymore; I am now the vehicle called Nova."
    )["ok"]
    assert not is_summarizer_refusal("Partly cloudy in Bangalore, around 28 degrees.")

    class _OkLLM:
        def clear(self):
            return None

        def generate(self, messages, max_tokens=48, quiet=True):
            return "It's partly cloudy in Bangalore, around 28 degrees.", object()

    class _BadLLM:
        def clear(self):
            return None

        def generate(self, messages, max_tokens=48, quiet=True):
            return "", object()

    class _RefuseLLM:
        def clear(self):
            return None

        def generate(self, messages, max_tokens=48, quiet=True):
            return "As an AI I cannot provide real-time news.", object()

    ok = summarize_evidence(
        _OkLLM(),
        "weather in Bangalore",
        "1. Skies partly cloudy, 28C.",
    )
    assert ok and "Bangalore" in ok
    assert summarize_evidence(_BadLLM(), "weather", "1. Skies partly cloudy.") is None
    assert summarize_evidence(_RefuseLLM(), "news in Delhi", "1. Delhi metro expands.") is None


def test_oem_router_calendar_email_search():
    gw = OemToolGateway()
    assert gw.route("What's on my calendar?") == "check_calendar"
    assert gw.route("Any unread email?") == "check_email"
    assert gw.route("Search the web for hailo") == "web_search"
    assert gw.route("Hello there") == "smalltalk"
    assert gw.route("Look up hailo news") == "web_search"
    # Plural must route: "meetings" is the canonical demo phrasing.
    assert gw.route("What meetings do I have today?") == "check_calendar"


def test_conversation_profile_declines_instead_of_enabling_all():
    """enabled=[] means no tools (it is falsy — must not fall back to all)."""
    gw = OemToolGateway(enabled=[])
    assert gw.route("What meetings do I have today?") == "capability_unavailable"
    assert gw.route("Can you check my email?") == "capability_unavailable"
    assert "calendar" in gw.execute("capability_unavailable",
                                    query="what meetings today")["speak"].lower()
    # Natural conversation must not be intercepted at all.
    assert gw.route("That's a long drive isn't it?") is None
    assert gw.route("I'm driving to Bangalore this evening.") is None


def test_identity_is_deterministic_never_web_search():
    """Identity turns bypass both web_search and the LLM (repetition-lock source)."""
    gw = OemToolGateway()
    for utt in (
        "What is your name?",
        "Hey there, what is your name and what can you do?",
        "Who are you?",
        "introduce yourself",
    ):
        assert gw.route(utt) == "identity", utt
    out = gw.execute("identity", query="who are you")
    assert out["ok"] is True
    assert "Nova" in out["speak"]


def test_oem_web_search_fail_closed_without_keys(monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    monkeypatch.delenv("BRAVE_API_KEY", raising=False)
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    gw = OemToolGateway()
    out = gw.execute("web_search", query="hailo news")
    assert out["ok"] is False
    assert out["status"] == "unavailable"
    assert "can't reach" in out["speak"].lower()




def test_oem_writes_disabled():
    gw = OemToolGateway(write_enabled=False)
    out = gw.execute("send_email", query="hi")
    assert out["ok"] is False


def test_config_profile_resolution(monkeypatch):
    from nova_hailo.config import ROOT, resolve_config_path

    monkeypatch.delenv("NOVA_HAILO_CONFIG", raising=False)
    monkeypatch.setenv("NOVA_HAILO_PROFILE", "oem")
    p = resolve_config_path()
    assert p == ROOT / "config.oem.yaml"
    monkeypatch.setenv("NOVA_HAILO_PROFILE", "oem_rollback")
    assert resolve_config_path() == ROOT / "config.oem_rollback.yaml"


def test_oem_config_yaml_exists():
    root = Path(__file__).resolve().parents[1]
    assert (root / "config.oem.yaml").is_file()
    assert (root / "config.oem_rollback.yaml").is_file()
