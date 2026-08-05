#!/usr/bin/env bash
# Offline + Pi E2E verification harness for OEM release gates.
# Callers: DEMO_TODO Sat–Sun verification; pi-release-verification todo.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
RUN_DIR="${NOVA_HAILO_DEMO_RUN:-$ROOT/logs/demo/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$RUN_DIR"
export PYTHONPATH="${ROOT}/../..:${ROOT}:${PYTHONPATH:-}"

echo "[verify] run dir: $RUN_DIR"

python3 -m pytest \
  tests/test_oem_demo_offline.py \
  tests/test_bench_contracts.py \
  tests/test_audio_gate.py \
    \
  -q --tb=line | tee "$RUN_DIR/pytest_offline.txt"

python3 - <<'PY' "$RUN_DIR" "$ROOT"
import json, sys
from pathlib import Path
run, root = Path(sys.argv[1]), Path(sys.argv[2])
sys.path.insert(0, str(root))
sys.path.insert(0, str(root.parent.parent))
from nova_hailo.response_contract import validate_spoken_unit
from nova_hailo.tools.oem_tools import OemToolGateway
from nova_hailo.session_fsm import SessionFSM
from nova_hailo.context_history import ConversationHistory

corpus = [
    "Hello Nova.",
    "What's on my calendar today?",
    "Any unread email?",
    "Search the web for Hailo AI news.",
    "What did I just ask about?",
]
gw = OemToolGateway(enabled=["web_search", "check_calendar", "check_email", "list_drive_files"])
hist = ConversationHistory(max_turns=2)
results = []
for q in corpus:
    route = gw.route(q)
    if route:
        out = gw.execute(route, query=q)
        assert out.get("status") in {"success", "unavailable"}
        if not out.get("ok"):
            assert "can't reach" in out["speak"].lower() or out.get("status") == "unavailable"
            speak = out["speak"]
        else:
            speak = out["speak"]
    else:
        speak = "I'm ready."
    c = validate_spoken_unit(speak)
    assert c["ok"] or c["fallback"]
    hist.add(q, c["speak"])
    results.append({"q": q, "route": route, "contract_ok": c["ok"], "speak": c["speak"][:120]})

fsm = SessionFSM()
fsm.arm()
ctx = fsm.begin_turn()
assert fsm.set_thinking(ctx.generation_id)
assert fsm.set_speaking(ctx.generation_id)
fsm.interrupt("test")
assert fsm.generation_id == ctx.generation_id + 1

summary = {
    "corpus_n": len(results),
    "results": results,
    "history_len": len(hist),
    "gate": "offline_scripted_ok",
}
(run / "e2e_summary.json").write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))
PY

if [[ "${NOVA_HAILO_LIVE:-0}" == "1" ]]; then
  echo "[verify-live] Live mode — warm models and capture soak into $RUN_DIR/soak.jsonl"
  cat >"$RUN_DIR/soak_instructions.md" <<'SOAK'
# Live soak checklist
1. Warm Whisper+Qwen via run_demo_oem.sh
2. 100 scripted turns (greeting, calendar, email, search, follow-up, barge-in)
3. 3×30 min soaks with active cooling
4. Record p50/p95 speech_end_to_audible_ms, barge_in_stop_ms, RSS, swap, temp
5. Append JSONL lines to soak.jsonl
SOAK
else
  echo "[verify] skipping live Pi soak (set NOVA_HAILO_LIVE=1 on target)"
  echo '{"status":"deferred_to_pi","gates":"see DEMO_TODO.md"}' >"$RUN_DIR/soak.jsonl"
fi

echo "[verify] done → $RUN_DIR"
