"""Headless runs of the Nova face (examples/realtime/lvgl); skipped until built."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "examples/realtime/lvgl"
BIN = APP / "build/nova_face"
pytestmark = pytest.mark.skipif(not BIN.exists(), reason="build examples/realtime/lvgl first")


def _run(script: Path, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [str(BIN), "--headless", "--script", str(script), *extra],
        capture_output=True, text=True, timeout=60,
    )


def test_demo_script_renders_every_state_and_exits_cleanly():
    r = _run(APP / "demo/states.jsonl")
    assert r.returncode == 0, r.stderr
    assert '{"wake":true}' in r.stdout            # the {"_tap": true} line


@pytest.mark.parametrize("extra", [("--size", "360x360", "--round"), ("--size", "1024x600")])
def test_round_and_wide_layouts_render(extra):
    r = _run(APP / "demo/states.jsonl", *extra)
    assert r.returncode == 0, r.stderr


def test_hostile_input_does_not_crash(tmp_path):
    lines = [
        '{"nova": "' + "long reply " * 1400 + '"}',          # ~15 KB
        '{"you": "emoji 😀 and ünïcödé"}',
        "{broken json",
        "",
        '["not", "an", "object"]',
        '{"state": "LISTENING"}',  # level is only applied while listening
        '{"level": 7}',
        '{"level": -1}',
        '{"state": "DANCING"}',
        '{"conn": 5}',
        '{"error": 42}',
    ]
    lines += ['{"state": "%s"}' % s for s in ("IDLE", "LISTENING", "THINKING", "SPEAKING") * 50]
    script = tmp_path / "hostile.jsonl"
    script.write_text("\n".join(lines) + "\n", encoding="utf-8")
    r = _run(script)
    assert r.returncode == 0, r.stderr


# --- Layout checks (F2b): --dump prints the widget tree as JSON lines. ---

def _dump(script: Path, *extra: str) -> list[dict]:
    r = _run(script, "--dump", *extra)
    assert r.returncode == 0, r.stderr
    return [json.loads(line) for line in r.stdout.splitlines() if line.strip()]


def _wh(extra: tuple[str, ...]) -> tuple[int, int]:
    w, h = extra[extra.index("--size") + 1].split("x")
    return int(w), int(h)


def _is_wide(w: int, h: int) -> bool:
    return w * 10 > h * 13                        # same rule as nova_face.c


def _visible(obj: dict, by_id: dict[int, dict]) -> bool:
    o = obj
    while True:
        if o["hidden"]:
            return False
        if o["parent"] == -1:
            return True
        o = by_id[o["parent"]]


def _covers_screen(o: dict, w: int, h: int) -> bool:
    return o["x1"] <= 0 and o["y1"] <= 0 and o["x2"] >= w - 1 and o["y2"] >= h - 1


def _box(o: dict) -> tuple[int, int, int, int]:
    return (o["x1"], o["y1"], o["x2"], o["y2"])


def _assert_no_clipping(objs: list[dict]) -> None:
    """Every visible object lies inside its parent's box, unless some ancestor
    between it and the screen has overflow visible (none do today; this is
    the general rule, checked defensively)."""
    by_id = {o["id"]: o for o in objs}
    for o in objs:
        if o["parent"] == -1 or not _visible(o, by_id):
            continue
        parent = by_id[o["parent"]]
        exempt, a = False, parent
        while True:
            if a["overflow"]:
                exempt = True
                break
            if a["parent"] == -1:
                break
            a = by_id[a["parent"]]
        if exempt:
            continue
        assert (o["x1"] >= parent["x1"] - 1 and o["x2"] <= parent["x2"] + 1 and
                o["y1"] >= parent["y1"] - 1 and o["y2"] <= parent["y2"] + 1), (
            f'{o["cls"]}#{o["id"]} {_box(o)} clips out of parent '
            f'{parent["cls"]}#{parent["id"]} {_box(parent)}'
        )


def _assert_inside_inscribed_circle(objs: list[dict], w: int, h: int) -> None:
    by_id = {o["id"]: o for o in objs}
    cx, cy = w / 2.0, h / 2.0
    r = min(w, h) / 2.0 + 1
    for o in objs:
        if not _visible(o, by_id) or _covers_screen(o, w, h):
            continue
        for x, y in ((o["x1"], o["y1"]), (o["x2"], o["y1"]),
                     (o["x1"], o["y2"]), (o["x2"], o["y2"])):
            dist = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
            assert dist <= r, (
                f'{o["cls"]}#{o["id"]} corner ({x},{y}) is {dist - r:.1f}px outside the '
                f'inscribed circle (r={r:.1f}, centre=({cx},{cy})), box {_box(o)}'
            )


def _assert_text_clears_orb(objs: list[dict], w: int, h: int) -> None:
    by_id = {o["id"]: o for o in objs}
    candidates = [o for o in objs if _visible(o, by_id) and o["radius"] >= 0x7FFF
                  and not _covers_screen(o, w, h)]
    assert candidates, "no visible circular orb found"
    orb = max(candidates, key=lambda o: (o["x2"] - o["x1"]) * (o["y2"] - o["y1"]))
    for o in objs:
        if o is orb or o["cls"] not in ("label", "button") or not _visible(o, by_id):
            continue
        overlap = not (o["x2"] < orb["x1"] or o["x1"] > orb["x2"] or
                        o["y2"] < orb["y1"] or o["y1"] > orb["y2"])
        assert not overlap, (
            f'{o["cls"]}#{o["id"]} {_box(o)} intersects the orb '
            f'#{orb["id"]} {_box(orb)}'
        )


_NOVA_LONG = ("It's 7:32 PM IST, Saturday 26 September. "
              "It's 7:32 PM IST, Saturday 26 September. "
              "It's 7:32 PM IST, Saturday 26 September.")

_LAYOUT_SCRIPT = "\n".join(json.dumps(ev) for ev in (
    {"conn": "up"},
    {"state": "LISTENING"},
    {"level": 0.5},
    {"you": "What time is it right now?"},
    {"nova": _NOVA_LONG},
    {"error": ""},
)) + "\n"


@pytest.mark.parametrize("extra", [
    ("--size", "240x240", "--round"),
    ("--size", "360x360", "--round"),
    ("--size", "480x480", "--round"),
    ("--size", "480x480"),
    ("--size", "1024x600"),
])
def test_layout_fits_every_panel_size(tmp_path, extra):
    script = tmp_path / "layout.jsonl"
    script.write_text(_LAYOUT_SCRIPT, encoding="utf-8")
    objs = _dump(script, *extra)
    w, h = _wh(extra)

    _assert_no_clipping(objs)
    if "--round" in extra:
        _assert_inside_inscribed_circle(objs, w, h)
    if not _is_wide(w, h):
        _assert_text_clears_orb(objs, w, h)


def test_conn_down_returns_to_idle(tmp_path):
    def dump(lines: list[str]) -> list[dict]:
        script = tmp_path / "conn.jsonl"
        script.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return _dump(script, "--size", "480x480")

    up = dump(['{"conn":"up"}', '{"state":"THINKING"}'])
    by_id_up = {o["id"]: o for o in up}
    assert any(o["cls"] == "arc" and _visible(o, by_id_up) for o in up)

    down = dump(['{"conn":"up"}', '{"state":"THINKING"}', '{"conn":"down"}'])
    by_id_down = {o["id"]: o for o in down}
    assert not any(o["cls"] == "arc" and _visible(o, by_id_down) for o in down)
