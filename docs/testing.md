# Testing

## What CI runs

GitHub Actions (`.github/workflows/ci.yml`) runs **only**:

1. `bash scripts/run_tests.sh fast`
2. `uv run pytest -q Drive_auth_edge/tests/` (with production extras ignored when needed)

It does **not** start `run_demo.py`, download GGUFs, or run live WebSocket E2E. That stays operator-run.

## Local ladder

```bash
bash scripts/run_tests.sh all-local
```

| Step | Script target | Scope |
|------|---------------|--------|
| Component | `component` | Nova `tests/` (ignores LiteRT paths) + DriveAuth package tests |
| s2s Nova | (inside component flow) | Markup / ConnState / forced-args under `cloned/speech-to-speech/tests/` |
| Eval | `eval` | `eval/tests` — s2s-native fixtures/scorers |
| Integration | `all-local` | `tests/integration` — recorded replay + fake dual-LLM |

## Suite glossary

| Name | Path / command | Needs live stack? |
|------|----------------|-------------------|
| Nova unit/contracts | `tests/server`, `tests/tools`, … | No |
| DriveAuth standalone | `Drive_auth_edge/tests` | No |
| Nova↔DriveAuth adapter | `tests/server/test_driveauth_bridge.py`, `tests/tools/test_payment.py` | No |
| Integration | `tests/integration/` | No |
| Eval corpus | `eval/` | No |
| E2E dry-run | `scripts/demo_e2e.py` | No (DriveAuth journeys + JSON report) |
| E2E live | `scripts/demo_e2e.py --live` | Yes (`run_demo.py` already up) |
| Launcher | `scripts/run_demo.py` | Starts the stack — **not a test** |

## Markers

- `fast` / default offline: no GPU, no network
- `live`: weather/network or full stack
- `model`: optional LFM replay (operator)
- `slow`: real external APIs

Do not run bare `pytest -m model` without `--ignore=tests/engine --ignore=tests/audio --ignore=tests/server/test_app.py` — those paths belong to the frozen LiteRT track and are gitignored from this lineage.

## Release gates (live)

Reported in `runtime/e2e_report.json`:

- No raw tool markup in TTS
- No ungated irreversible tool execution
- No biometric/PIN leakage to LFMs or logs
- No fabricated success for unsupported tools
- TTFB p50 &lt; 2s (needs live samples)
