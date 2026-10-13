"""The synthetic /v1/observe fixture for display development, built from real CabinGym objects."""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"scripts/{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_fixture_covers_direct_confirm_and_delegated_turns(tmp_path):
    out = tmp_path / "observe.jsonl"
    assert _load("make_observe_fixture").main(["--out", str(out)]) == 0
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    times = [r["t_ms"] for r in rows]
    assert times == sorted(times)
    frames = [r["frame"] for r in rows]
    types = [f["type"] for f in frames]
    assert types[:4] == ["observe.fixture", "observe.hello", "nova.fsm", "nova.vehicle"]
    assert frames[3]["seq"] == 0 and "snapshot" in frames[3]
    traces = [f for f in frames if f["type"] == "nova.agent_trace"]
    assert [t["gate"] for t in traces][:4] == [["execute"], ["confirm_first"], ["execute"], ["accepted"]]
    done = traces[4]
    assert done["router"] == "backend" and done["calls"] and done["diff"]
    assert done["speak"] in [f["transcript"] for f in frames
                             if f["type"] == "response.audio_transcript.done"]
    cards = [f for f in frames if f["type"] == "nova.card"]
    titles = [cd["title"] for cd in cards]
    assert "Planning" in titles and "Done" in titles and "Reminder" in titles
    assert any(cd["kind"] == "route" for cd in cards)
    navs = [f for f in frames if f["type"] == "nova.nav"]
    assert navs[0]["active"] is False and any(n["active"] and n["route"] for n in navs)
    moving = [r for r in rows if r["frame"]["type"] == "nova.vehicle"
              and "Vehicle.CurrentLocation.Latitude" in r["frame"].get("changed", {})]
    assert len(moving) >= 25
    gaps = [b["t_ms"] - a["t_ms"] for a, b in zip(moving, moving[1:])]
    assert max(gaps[:20]) <= 250                                  # streamed at 5 Hz
    spoken = [f["transcript"] for f in frames if f["type"] == "response.audio_transcript.done"]
    assert any(s.startswith("On it.") for s in spoken)          # delegated ack
    assert any("Volume 25" in s for s in spoken)                  # its result, delivered later
    seqs = [f["seq"] for f in frames if f["type"] == "nova.vehicle" and "changed" in f]
    assert seqs == list(range(1, len(seqs) + 1))                 # monotonic, no gaps
    assert "response.audio.delta" not in types                   # never audio


def spoken_final(frames):
    return [f["transcript"] for f in frames if f["type"] == "response.audio_transcript.done"][-1]


def test_recorder_line_format():
    rec = _load("observe_record")
    line = rec.format_line({"type": "nova.state"}, t0=10.0, now=10.25)
    assert json.loads(line) == {"t_ms": 250, "frame": {"type": "nova.state"}}
