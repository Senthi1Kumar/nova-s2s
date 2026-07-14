from nova.launch.prewarm import build_prewarm_payload, prewarm_llm


def test_payload_has_system_and_tools_and_caps_tokens():
    payload = build_prewarm_payload("SOULTEXT", [{"type": "function", "name": "x",
        "description": "d", "parameters": {"type": "object", "properties": {}}}])
    assert payload["max_tokens"] == 1
    assert payload["messages"][0]["role"] == "system"
    assert "SOULTEXT" in payload["messages"][0]["content"]
    assert payload["tools"][0]["function"]["name"] == "x"  # chat-completions nested shape


def test_prewarm_llm_never_raises_on_unreachable_server():
    # best-effort warm: startup must never block/crash on a bad/dead base_url
    prewarm_llm("http://127.0.0.1:1", "SOULTEXT", [])
