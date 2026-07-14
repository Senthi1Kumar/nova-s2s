from scripts.run_demo import resolve_stt_device


def test_prefers_configured_cuda():
    assert resolve_stt_device({"stt_device": "cuda"}) == "cuda"


def test_defaults_to_cpu_when_unset():
    assert resolve_stt_device({}) == "cpu"


def test_explicit_cpu_respected():
    assert resolve_stt_device({"stt_device": "cpu"}) == "cpu"


def test_nova_config_env(monkeypatch, tmp_path):
    from pathlib import Path
    import scripts.run_demo as rd

    alt = tmp_path / "alt.yaml"
    alt.write_text("llm_profile: x\nstt: sensevoice\ntts: kokoro\n")
    monkeypatch.setenv("NOVA_CONFIG", str(alt))
    assert rd._config_path() == alt
