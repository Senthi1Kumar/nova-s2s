import io
import json
import wave

import numpy as np
import pytest

from bench.voice.bench_decision import decision_summary
from bench.voice.bench_voice import read_pcm16_wav
from bench.voice.score import (
    normalize_transcript,
    percentile,
    score_manifest,
    word_error_rate,
)


def test_normalize_transcript_numbers_and_punctuation():
    assert normalize_transcript("Twenty-one, please!") == "21 please"
    assert normalize_transcript("I have ONE hundred and two") == "i have 102"
    assert normalize_transcript("  Hello,   BOX. ") == "hello box"


def test_wer_uses_word_edit_distance():
    assert word_error_rate("set fan to 2", "set fan to two") == 0.0
    assert word_error_rate("turn on the fan", "turn off fan") == 0.5
    assert word_error_rate("", "noise") == 1.0
    assert word_error_rate("", "") == 0.0


def test_percentile_interpolates_and_handles_empty():
    assert percentile([], 50) is None
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([1, 2, 3, 4], 95) == pytest.approx(3.85)


def test_score_manifest_aggregates_wer_and_latency(tmp_path):
    manifest = tmp_path / "manifest.json"
    results = tmp_path / "results.json"
    manifest.write_text(json.dumps({
        "clips": [
            {"id": "a", "reference": "twenty one", "scenario": "numbers"},
            {"id": "b", "reference": "hello box", "scenario": "clean"},
            {"id": "missing", "reference": "not run", "scenario": "clean"},
        ]
    }))
    results.write_text(json.dumps({
        "clips": [
            {"id": "a", "transcript": "21", "timings": {"asr_ms": 100, "first_audio_ms": 500}},
            {"id": "b", "transcript": "hello there", "timings": {"asr_ms": 300, "first_audio_ms": 700}},
        ]
    }))
    report = score_manifest(manifest, results)
    assert report["clips_completed"] == 2
    assert report["clips_missing"] == 1
    assert report["wer"] == pytest.approx(1 / 3)
    assert report["latency_ms"]["asr_ms"]["p50"] == 200
    assert report["latency_ms"]["first_audio_ms"]["p95"] == 690
    assert report["unavailable_metrics"] == ["cpu_percent", "rss_mb"]


def test_read_pcm16_wav_requires_benchmark_format(tmp_path):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x01\x00" * 320)
    path = tmp_path / "clip.wav"
    path.write_bytes(buf.getvalue())
    assert len(read_pcm16_wav(path)) == 640

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\x00\x00" * 640)
    path.write_bytes(buf.getvalue())
    with pytest.raises(ValueError, match="mono PCM16 at 16 kHz"):
        read_pcm16_wav(path)


def test_decision_summary_separates_cold_warm_and_parity():
    records = [
        {"latency_ms": 900, "rss_mb": 1200, "matches_expected": True},
        {"latency_ms": 100, "rss_mb": 1250, "matches_expected": True},
        {"latency_ms": 300, "rss_mb": 1300, "matches_expected": False},
    ]
    report = decision_summary(records)
    assert report["cold_ms"] == 900
    assert report["warm_ms"]["p50"] == 200
    assert report["warm_ms"]["p95"] == 290
    assert report["rss_mb_max"] == 1300
    assert report["answer_parity"] == pytest.approx(2 / 3)


def test_answers_match_compares_noul_decisions_not_raw_probabilities():
    from bench.voice.bench_decision import answers_match

    assert answers_match({"turn_complete": {"type": "noul", "noul": 0.8}},
                         {"turn_complete": True})
    assert not answers_match({"turn_complete": {"type": "noul", "noul": 0.2}},
                             {"turn_complete": True})


def _write_wav(path, samples, channels=1, rate=16000):
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(np.asarray(samples, dtype="<i2").tobytes())


def test_false_trigger_rate_and_per_10_min():
    from bench.voice.score import false_trigger_rate

    clips = [{"triggered": t, "duration_s": 10.0} for t in (True, False, False, False, True, False)]
    stats = false_trigger_rate(clips)
    assert stats["triggers"] == 2
    assert stats["rate"] == pytest.approx(2 / 6)
    assert stats["per_10_min"] == pytest.approx(20.0)  # 2 triggers in 60 s of audio
    assert false_trigger_rate([])["per_10_min"] is None


def test_score_manifest_splits_kinds_and_conditions(tmp_path):
    manifest = tmp_path / "m.json"
    results = tmp_path / "r.json"
    manifest.write_text(json.dumps({"clips": [
        {"id": "p1", "reference": "hello box", "condition": "quiet", "kind": "prompt"},
        {"id": "p2", "reference": "hello box", "condition": "music", "kind": "prompt"},
        {"id": "n1", "reference": "", "condition": "quiet", "kind": "negative"},
        {"id": "n2", "reference": "", "condition": "music", "kind": "negative"},
        {"id": "n3", "reference": "", "condition": "meeting", "kind": "negative"},
    ]}))
    results.write_text(json.dumps({"clips": [
        {"id": "p1", "transcript": "hello box", "bytes_ok": True, "timings": {"asr_ms": 100}},
        {"id": "p2", "transcript": "hello", "bytes_ok": True, "timings": {"asr_ms": 200}},
        {"id": "n1", "transcript": "", "ignored": {"reason": "no_speech"}},
        {"id": "n2", "transcript": "thank you"},
        {"id": "n3", "transcript": "", "ignored": None},
    ]}))
    report = score_manifest(manifest, results)
    assert report["wer"] == pytest.approx(1 / 4)
    assert report["conditions"]["quiet"]["wer"] == 0
    assert report["conditions"]["music"]["wer"] == pytest.approx(0.5)
    assert report["false_trigger"]["triggers"] == 1
    assert report["conditions"]["music"]["false_trigger"]["triggers"] == 1
    assert report["conditions"]["meeting"]["false_trigger"]["triggers"] == 0
    assert report["bytes_ok_rate"] == 1.0


def _summary(quiet_wer, music_wer, triggers, bytes_ok=1.0):
    clip = {"negatives": 6, "minutes": 1.0}
    return {
        "bytes_ok_rate": bytes_ok,
        "errors": 0,
        "conditions": {
            "quiet": {"wer": quiet_wer, "false_trigger": {**clip, "triggers": triggers}},
            "music": {"wer": music_wer, "false_trigger": {**clip, "triggers": 0}},
            "meeting": {"wer": 0.9, "false_trigger": {**clip, "triggers": 50}},
        },
    }


def test_gates_pass_and_fail_by_threshold():
    from bench.voice.score import evaluate_gates, format_gates

    gates = evaluate_gates(_summary(0.10, 0.20, 0))
    assert [g["passed"] for g in gates] == [True] * 5
    assert "FAIL" not in format_gates(gates)

    assert not evaluate_gates(_summary(0.11, 0.20, 0))[0]["passed"]
    assert not evaluate_gates(_summary(0.10, 0.21, 0))[1]["passed"]
    # 3 triggers over 2 min of quiet+music negatives = 15 per 10 min; meeting is not gated
    assert not evaluate_gates(_summary(0.05, 0.05, 3))[2]["passed"]
    assert not evaluate_gates(_summary(0.05, 0.05, 0, bytes_ok=0.98))[4]["passed"]
    assert "FAIL" in format_gates(evaluate_gates(_summary(0.5, 0.05, 0)))


def test_gates_report_no_data_instead_of_passing():
    from bench.voice.score import evaluate_gates, format_gates

    gates = evaluate_gates({})
    assert all(g["passed"] is None for g in gates)
    assert "NO DATA" in format_gates(gates)
    assert format_gates(gates).endswith("FAIL")


def test_compare_prints_side_by_side(tmp_path, capsys):
    from bench.voice.score import main

    for name, wer in (("before", 0.3), ("after", 0.1)):
        (tmp_path / f"{name}.json").write_text(json.dumps({"summary": {"wer": wer}}))
    assert main(["--compare", str(tmp_path / "before.json"), str(tmp_path / "after.json")]) == 0
    line = next(row for row in capsys.readouterr().out.splitlines() if row.startswith("WER (all"))
    assert line.split()[-2:] == ["0.300", "0.100"]


def test_make_manifest_splits_channels_and_chops_negatives(tmp_path):
    from bench.voice.make_manifest import build

    corpus = tmp_path / "corpus"
    (corpus / "audio").mkdir(parents=True)
    # per-channel mono files as record_asr_corpus.py writes them
    for ch, level in ((0, 100), (1, 200)):
        _write_wav(corpus / "audio" / f"sp_001__quiet__ch{ch}.wav", [level] * 1600)
    _write_wav(corpus / "audio" / "sp_001__music__ch0.wav", [7] * 1600)
    (corpus / "prompts.jsonl").write_text(json.dumps({
        "id": "sp_001", "reference": "Set the fan.", "condition": "quiet",
        "audio_paths": {
            "quiet/ch0": "audio/sp_001__quiet__ch0.wav", "quiet/ch1": "audio/sp_001__quiet__ch1.wav",
            "music/ch0": "audio/sp_001__music__ch0.wav",
        },
    }) + "\n")
    # 25 s two-channel negative: ch0 = 1, ch1 = 2
    stereo = np.tile(np.array([1, 2], dtype="<i2"), 16000 * 25)
    _write_wav(tmp_path / "neg.wav", stereo, channels=2)
    out = tmp_path / "run"

    manifest = json.loads(build(out, corpus, [("music", tmp_path / "neg.wav")], "ch1").read_text())
    clips = manifest["clips"]
    prompts = [c for c in clips if c["kind"] == "prompt"]
    negatives = [c for c in clips if c["kind"] == "negative"]
    assert [p["id"] for p in prompts] == ["sp_001__quiet"]  # music has no ch1 take
    assert prompts[0] | {"path": None} == {
        "id": "sp_001__quiet", "path": None, "reference": "Set the fan.",
        "condition": "quiet", "channel": "ch1", "kind": "prompt",
    }
    assert set(read_wav_samples(out / prompts[0]["path"])) == {200}
    assert [n["duration_s"] for n in negatives] == [10.0, 10.0, 5.0]
    assert all(n["condition"] == "music" and n["reference"] == "" for n in negatives)
    assert set(read_wav_samples(out / negatives[0]["path"])) == {2}
    assert read_pcm16_wav(out / negatives[2]["path"]) == b"\x02\x00" * 80000

    beam = json.loads(build(out, corpus, [], "ch0", label="beam", boards={"music"}).read_text())
    assert [c["id"] for c in beam["clips"]] == ["sp_001__music"]
    assert (out / "manifest-beam.json").exists()
    assert beam["clips"][0]["channel"] == "beam"


def read_wav_samples(path):
    return np.frombuffer(read_pcm16_wav(path), dtype="<i2").tolist()


def test_make_manifest_resamples_and_splits_multichannel_prompt(tmp_path):
    from bench.voice.make_manifest import channel_samples

    _write_wav(tmp_path / "s.wav", np.tile(np.array([5, 9], dtype="<i2"), 800), channels=2, rate=8000)
    out = channel_samples(tmp_path / "s.wav", 1)
    assert len(out) == 1600
    assert set(out.tolist()) == {9}


def _play(events, stopped=10.0):
    from bench.voice.bench_voice import TurnRecorder

    turn = TurnRecorder(stopped)
    for at, item in events:
        if isinstance(item, int):
            turn.binary(item, stopped + at)
        else:
            turn.event(item, stopped + at)
    return turn.record({"id": "c1", "kind": "prompt", "condition": "quiet", "channel": "ch1"}, 1.0)


def test_turn_recorder_assembles_result_with_byte_accounting():
    record = _play([
        (0.05, {"type": "transcript_partial", "text": "se"}),
        (0.30, {"type": "transcript", "text": "set the fan"}),
        (0.50, {"type": "token", "text": "Ok"}),
        (0.60, {"type": "tts_start", "bytes": 2560}),
        (0.65, 1280),
        (0.70, 1280),
        (0.71, {"type": "tts_end", "bytes": 2560}),
        (0.80, {"type": "tts_start", "bytes": 1280}),
        (0.85, 1280),
        (0.86, {"type": "tts_end", "bytes": 1280}),
        (0.90, {"type": "done", "text": "Ok.", "timings": {"asr_ms": 120.0, "llm_ttft_ms": 80.0}}),
    ])
    assert record["status"] == "ok"
    assert record["transcript"] == "set the fan"
    assert record["reply"] == "Ok."
    assert record["ignored"] is None
    assert record["bytes_ok"] is True
    assert (record["sentences"], record["bytes_announced"], record["bytes_received"]) == (2, 3840, 3840)
    assert record["timings"]["first_audio_ms"] == pytest.approx(650)
    assert record["timings"]["total_ms"] == pytest.approx(900)
    assert record["timings"]["asr_ms"] == 120.0
    assert (record["kind"], record["condition"], record["channel"]) == ("prompt", "quiet", "ch1")


def test_turn_recorder_flags_short_sentence_and_handles_ignored_and_timeout():
    short = _play([
        (0.1, {"type": "tts_start", "bytes": 2560}),
        (0.2, 1280),
        (0.3, {"type": "tts_end", "bytes": 2560}),
        (0.4, {"type": "done", "text": "x", "timings": {}}),
    ])
    assert short["bytes_ok"] is False

    ignored = _play([(0.2, {"type": "ignored", "reason": "no_speech", "speech_ms": 40, "ratio": 0.02})])
    assert ignored["status"] == "ok"
    assert ignored["ignored"] == {"reason": "no_speech", "speech_ms": 40, "ratio": 0.02}
    assert ignored["transcript"] == ""

    assert _play([(0.1, {"type": "transcript", "text": "hi"})])["status"] == "error"
    failed = _play([(0.1, {"type": "error", "error": {"code": "boom"}})])
    assert failed["status"] == "error" and "boom" in failed["error"]


def test_stream_url_and_in_process_client(tmp_path):
    from websockets.sync.server import serve

    from bench.voice.bench_voice import run_clip, stream_url

    assert stream_url("http://127.0.0.1:8442") == "ws://127.0.0.1:8442/v1/voice/stream"
    assert stream_url("https://box.lan/") == "wss://box.lan/v1/voice/stream"
    assert stream_url("ws://h:1/custom") == "ws://h:1/custom"

    seen = {}

    def handler(ws):
        seen["auth"] = ws.request.headers.get("Authorization")
        seen["start"] = json.loads(ws.recv())
        audio = 0
        while True:
            message = ws.recv()
            if isinstance(message, bytes):
                audio += len(message)
            elif json.loads(message)["type"] == "stop":
                break
        seen["audio"] = audio
        ws.send(json.dumps({"type": "transcript", "text": "hello"}))
        ws.send(json.dumps({"type": "tts_start", "bytes": 1280}))
        ws.send(b"\x00" * 1280)
        ws.send(json.dumps({"type": "tts_end", "bytes": 1280}))
        ws.send(json.dumps({"type": "done", "text": "hi", "timings": {"asr_ms": 5}}))

    wav = tmp_path / "c.wav"
    _write_wav(wav, [0] * 3200)
    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        import threading

        threading.Thread(target=server.serve_forever, daemon=True).start()
        record = run_clip(f"http://127.0.0.1:{port}", {"id": "c", "audio": str(wav)}, "m", 5.0,
                          realtime=True, api_key="k")
        server.shutdown()
    assert seen["start"]["protocol"] == 2
    assert seen["auth"] == "Bearer k"
    assert seen["audio"] == 6400
    assert record["status"] == "ok" and record["bytes_ok"] and record["transcript"] == "hello"


def test_false_trigger_gate_needs_both_quiet_and_music_negatives():
    from bench.voice.score import evaluate_gates

    neg = {"negatives": 6, "minutes": 1.0, "triggers": 0}
    for present in ("quiet", "music"):
        summary = {"conditions": {present: {"wer": 0.0, "false_trigger": neg}}}
        gate = next(g for g in evaluate_gates(summary) if g["gate"].startswith("False"))
        assert gate["passed"] is None


def test_errored_clips_are_not_counted_clean(tmp_path):
    from bench.voice.score import evaluate_gates

    manifest = tmp_path / "m.json"
    results = tmp_path / "r.json"
    manifest.write_text(json.dumps({"clips": [
        {"id": "n1", "condition": "quiet", "kind": "negative"},
        {"id": "n2", "condition": "music", "kind": "negative"},
        {"id": "n3", "condition": "music", "kind": "negative"},
    ]}))
    results.write_text(json.dumps({"clips": [
        {"id": "n1", "transcript": "", "status": "ok", "bytes_ok": True},
        {"id": "n2", "transcript": "", "status": "error", "bytes_ok": True},
        {"id": "n3", "transcript": "", "status": "error", "bytes_ok": True},
    ]}))
    report = score_manifest(manifest, results)
    assert report["errors"] == 2
    assert "music" not in report["conditions"]
    assert report["bytes_ok_rate"] == 1.0
    gates = {g["gate"]: g for g in evaluate_gates(report)}
    assert gates["errors"]["passed"] is False
    assert gates["False triggers per 10 min"]["passed"] is None


def test_make_manifest_default_boards_exclude_route_suffixes(tmp_path):
    from bench.voice.make_manifest import build

    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_wav(corpus / "a.wav", [3] * 1600)
    (corpus / "prompts.jsonl").write_text(json.dumps({
        "id": "sp_001", "reference": "x",
        "audio_paths": {"music/ch0": "a.wav", "music-beam/ch0": "a.wav", "odd/ch0": "a.wav"},
    }) + "\n")
    clips = json.loads(build(tmp_path / "run", corpus, [], "ch0").read_text())["clips"]
    assert [c["id"] for c in clips] == ["sp_001__music"]
    beam = json.loads(build(tmp_path / "run", corpus, [], "ch0", "beam", {"music-beam"}).read_text())
    assert [c["id"] for c in beam["clips"]] == ["sp_001__music-beam"]
    assert beam["clips"][0]["condition"] == "music"


def test_turn_recorder_records_vad_and_failed_is_sticky():
    ok = _play([
        (0.1, {"type": "session", "turn_id": 1, "vad": "on"}),
        (0.2, {"type": "done", "text": "x", "timings": {}, "vad": "on"}),
    ])
    assert ok["vad"] == "on"
    failed = _play([
        (0.1, {"type": "session", "turn_id": 1, "vad": "on"}),
        (0.2, {"type": "transcript", "text": "hi", "vad": "failed"}),
        (0.3, {"type": "done", "text": "x", "timings": {}, "vad": "on"}),
    ])
    assert failed["vad"] == "failed"
    assert _play([(0.1, {"type": "done", "text": "x", "timings": {}})])["vad"] is None


def _vad_run(tmp_path, states):
    manifest = tmp_path / "m.json"
    results = tmp_path / "r.json"
    manifest.write_text(json.dumps({"clips": [{"id": f"c{i}", "condition": "quiet"} for i in range(len(states))]}))
    results.write_text(json.dumps({"clips": [
        {"id": f"c{i}", "transcript": "", "status": "ok", "vad": state} for i, state in enumerate(states)
    ]}))
    return score_manifest(manifest, results)


def test_score_summarizes_vad_and_flags_invalid_runs(tmp_path, capsys):
    from bench.voice.score import evaluate_gates, main, vad_warning

    clean = _vad_run(tmp_path, ["on", "on"])
    assert clean["vad"] == {"on": 2}
    assert vad_warning(clean) is None
    assert next(g for g in evaluate_gates(clean) if g["gate"].startswith("VAD"))["passed"] is True

    mixed = _vad_run(tmp_path, ["on", "off"])
    assert mixed["vad"] == {"off": 1, "on": 1}
    assert "mixed" in vad_warning(mixed)
    assert next(g for g in evaluate_gates(mixed) if g["gate"].startswith("VAD"))["passed"] is False

    failed = _vad_run(tmp_path, ["on", "failed"])
    assert "failed" in vad_warning(failed)
    assert next(g for g in evaluate_gates(failed) if g["gate"].startswith("VAD"))["passed"] is False

    off = _vad_run(tmp_path, ["off", "off"])
    assert vad_warning(off) is None

    (tmp_path / "bad.json").write_text(json.dumps({"summary": failed}))
    assert main([str(tmp_path / "bad.json")]) == 1
    out = capsys.readouterr().out
    assert "WARNING" in out and "VAD: failed=1, on=1" in out
    assert out.rstrip().endswith("FAIL")
