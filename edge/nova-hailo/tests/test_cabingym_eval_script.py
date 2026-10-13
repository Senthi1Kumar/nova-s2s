# tests/test_cabingym_eval_script.py
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("cabingym_eval", ROOT / "scripts/cabingym_eval.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_regex_demo_subset_is_perfect(tmp_path):
    assert _load().main(["--router", "regex", "--tag", "demo", "--out", str(tmp_path)]) == 0
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["acc"] == 1.0 and summary["gate_acc"] == 1.0
    rows = (tmp_path / "trajectories.jsonl").read_text().splitlines()
    assert len(rows) == summary["n"] and json.loads(rows[0])["task"]


def test_oracle_full_set_is_perfect(tmp_path):
    assert _load().main(["--router", "oracle", "--out", str(tmp_path)]) == 0
    assert json.loads((tmp_path / "summary.json").read_text())["acc"] == 1.0
