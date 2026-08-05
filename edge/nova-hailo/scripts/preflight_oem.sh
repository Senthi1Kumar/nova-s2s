#!/usr/bin/env bash
# OEM preflight: models, OAuth presence, search keys, network — never prints secrets.
# Callers: DEMO_TODO operator runbook; freeze-rehearse todo.
# Exit 0 when fail=0. WARN lines do NOT stop the demo — chat still runs; tools fail closed.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
RUN_DIR="${1:-$ROOT/logs/demo/$(date +%Y%m%d-%H%M%S)}"
mkdir -p "$RUN_DIR"

ok=0
warn=0
fail=0
note() { echo "[preflight] $*"; }
pass() { note "OK  $*"; ok=$((ok + 1)); }
warn_() { note "WARN $*"; warn=$((warn + 1)); }
fail_() { note "FAIL $*"; fail=$((fail + 1)); }

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
  pass ".env present"
elif [[ -f "$ROOT/../../.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/../../.env"
  set +a
  pass "repo-root .env present"
else
  warn_ ".env missing — copy template:  cp .env.example .env && nano .env"
  if [[ -f "$ROOT/.env.example" ]]; then
    note "hint: .env.example is present in $ROOT"
  else
    warn_ ".env.example also missing (rsync the OEM tree)"
  fi
fi

has_nonempty() {
  local v="${!1:-}"
  [[ -n "$v" ]]
}

if has_nonempty BRAVE_API_KEY; then
  pass "BRAVE_API_KEY set (${#BRAVE_API_KEY} chars)"
else
  warn_ "BRAVE_API_KEY unset (web_search primary unavailable)"
fi
if has_nonempty SERPER_API_KEY; then
  pass "SERPER_API_KEY set (${#SERPER_API_KEY} chars)"
else
  warn_ "SERPER_API_KEY unset (Brave→Serper fallback unavailable)"
fi
if ! has_nonempty BRAVE_API_KEY && ! has_nonempty SERPER_API_KEY; then
  warn_ "no search keys — web_search will fail closed"
fi
if has_nonempty TAVILY_API_KEY; then
  pass "TAVILY_API_KEY set (${#TAVILY_API_KEY} chars)"
else
  warn_ "TAVILY_API_KEY unset (deep_research will fail closed)"
fi

TOK_CANDIDATES=(
  "${GOOGLE_OAUTH_TOKEN_PATH:-}"
  "$ROOT/runtime/google_oauth/tokens.json"
  "$HOME/.nova/google_token.json"
  "$HOME/.config/nova/google_token.json"
  "$ROOT/.google_token.json"
)
tok_found=0
for t in "${TOK_CANDIDATES[@]}"; do
  [[ -z "$t" ]] && continue
  # Relative token paths are relative to ROOT
  if [[ "$t" != /* ]]; then
    t="$ROOT/$t"
  fi
  if [[ -f "$t" ]]; then
    pass "Google token file present: ${t#$ROOT/}"
    tok_found=1
    break
  fi
done
if [[ "$tok_found" -eq 0 ]]; then
  warn_ "Google OAuth token missing — Workspace tools will fail closed"
  note "hint: set GOOGLE_OAUTH_* in .env, then complete one OAuth login so"
  note "      runtime/google_oauth/tokens.json contains a refresh_token"
fi

if curl -fsS --max-time 3 https://www.google.com/generate_204 >/dev/null 2>&1; then
  pass "network reachability (google 204)"
else
  fail_ "network unreachable"
fi
if curl -fsS --max-time 3 https://api.search.brave.com >/dev/null 2>&1 \
  || curl -fsS --max-time 3 https://google.serper.dev >/dev/null 2>&1; then
  pass "search API host reachable"
else
  warn_ "search API hosts not reachable"
fi

python3 - <<'PY' "$RUN_DIR" "$ROOT" || true
import hashlib, json, sys
from pathlib import Path
run, root = Path(sys.argv[1]), Path(sys.argv[2])
cands = [
    "models/Qwen2-1.5B-Instruct.hef",
    "models/whisper_base.hef",
    "models/Whisper-Base.hef",
    "models/piper/en_US-amy-low.onnx",
    "config.oem.yaml",
    "config.oem_rollback.yaml",
]
out = {"files": {}}
for rel in cands:
    p = root / rel
    if not p.is_file():
        continue
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    out["files"][rel] = {"sha256": h.hexdigest(), "bytes": p.stat().st_size}
(run / "inventory.json").write_text(json.dumps(out, indent=2))
print(f"[preflight] wrote {run / 'inventory.json'} ({len(out['files'])} files)")
PY

if [[ -f "$ROOT/config.oem.yaml" ]]; then
  pass "config.oem.yaml present"
else
  fail_ "config.oem.yaml missing"
fi
if [[ -f "$ROOT/config.oem_rollback.yaml" ]]; then
  pass "config.oem_rollback.yaml present"
else
  fail_ "config.oem_rollback.yaml missing"
fi

if command -v hailortcli >/dev/null 2>&1; then
  ver="$(hailortcli --version 2>/dev/null | head -1 || true)"
  note "hailortcli: ${ver:-unknown}"
  if echo "$ver" | grep -q "5.1.1"; then
    pass "HailoRT 5.1.1"
  else
    warn_ "expected HailoRT 5.1.1 — got: ${ver:-unknown}"
  fi
else
  warn_ "hailortcli not on PATH (run on Pi)"
fi

summary="$RUN_DIR/preflight_summary.txt"
{
  echo "ok=$ok warn=$warn fail=$fail"
  echo "profile=${NOVA_HAILO_PROFILE:-oem}"
  date -Is
} >"$summary"
note "summary → $summary (ok=$ok warn=$warn fail=$fail)"

if [[ "$fail" -eq 0 ]]; then
  note "PASS — preflight did not fail (WARN ≠ stop)."
  if [[ "$warn" -gt 0 ]]; then
    note "Chat/voice still OK. Tools without keys will say they can't reach the service."
    note "Next:  cp .env.example .env && edit keys, then re-run ./scripts/preflight_oem.sh"
    note "Then:  ./scripts/run_demo_oem.sh"
  else
    note "Next:  ./scripts/run_demo_oem.sh"
  fi
  exit 0
fi
note "FAIL — fix FAIL lines above before demo day."
exit 1
