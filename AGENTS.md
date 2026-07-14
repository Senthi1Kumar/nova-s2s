# AGENTS.md

Guidance for agents working in this repository.

## Runtime (adopted)

Nova s2s is a cascaded on-device voice stack:

SenseVoice → DriveAuth precheck (payments) → LFM 230M tool agent → tools → LFM 350M articulator → Kokoro.

Launcher: `scripts/run_demo.py`. Config: `nova/config.yaml`, `nova/launch/models.yaml`, `.env` from `.env.example`.

Do not import or revive the LiteRT / LiteRT-LM track (`nova/engine`, `nova/server/app.py`). It is excluded from this lineage.

## DriveAuth

Package: `Drive_auth_edge` (Parth / couder-04). Nova owns only `nova/server/driveauth_bridge.py` and payment wiring. Do not copy Trust/Risk/policy logic into Nova.

## Verification

- Offline: `bash scripts/run_tests.sh all-local`
- CI: fast + DriveAuth package + all-local (no live demo)
- Live: operator runs `run_demo.py`, then `demo_e2e.py --live`

See [docs/testing.md](docs/testing.md), [docs/pipeline.md](docs/pipeline.md), [docs/setup.md](docs/setup.md).

## Standing constraints

- Do not commit `.env`, GGUFs, `llama-server` binaries, or `runtime/`.
- Do not `git commit` unless the user explicitly asks; prefer proposing the message and file list.
- User runs long GPU/demo jobs and pastes output back.
- Pin fastapi / starlette / uvicorn together when touching HTTP deps.
