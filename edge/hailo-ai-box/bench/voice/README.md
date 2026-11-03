# Voice quality and latency benchmark

This harness drives the real `/v1/voice/stream` WebSocket. It does not use fake ASR, chat, or TTS services and contains no wake-word benchmark implementation.

## Corpus

Copy `runtime/voice-corpus/manifest.example.json` to a new manifest under the git-ignored runtime directory. Replace every path with a real held-out 16 kHz mono PCM16 WAV. Keep training/calibration clips out of the evaluation manifest.

Include both microphone paths and scenarios for unfinished pauses, holding floor, backchannels, genuine interruption, side-talk, music/noise, and independent laptop meeting playback. Record the microphone and processing route in the output directory name or run metadata.

## Run

With the real AI Box server running:

```bash
.venv/bin/python -m bench.voice.bench_voice \
  --manifest runtime/voice-corpus/manifest.json \
  --output runtime/results/voice-baseline-xvf3800.json
```

For Web Studio over HTTPS, use the matching `wss://` URL and API authentication required by the deployment. The default is loopback HTTP on the Pi.

The output contains one result per clip plus corpus-level WER and p50/p95 latency. Metrics that were not measured are listed as unavailable rather than filled with zero. Number-aware normalization prevents `twenty one` versus `21` from counting as an error.

Measure the unmodified cascade before evaluating a new pipeline. Compare timeout and D3 semantic turn control on the same clips. D3 Pi CPU latency/RSS and hosted-Jev comparisons are separate explicit benchmark runs; hosted Jev is never required for offline operation.

```bash
# Local D3; uses an already downloaded model or fetches the selected HF revision.
.venv/bin/python -m bench.voice.bench_decision \
  --suite runtime/voice-corpus/turn-judgments.json \
  --output runtime/results/d3-edge-turn-judgments.json --repeat 3

# Optional hosted comparison only; never required by the offline pipeline.
# TYPESAFE_API_KEY must already be present in the environment.
.venv/bin/python -m bench.voice.bench_decision --backend jev \
  --suite runtime/voice-corpus/turn-judgments.json \
  --output runtime/results/jev-turn-judgments.json --repeat 3
```

## Verification status

The RPi currently passes 182 non-hardware tests and all 12 Hailo hardware tests. No live per-mic WER/latency baseline or D3 Pi result has been recorded yet. Do not describe those measurements as complete until the operator has run the full setup and the real Studio scenarios.

## Live prompt-read eval

Picks the XVF3800 channel and measures the speech gate before and after the fix. Recordings are personal voice: keep them under the git-ignored `runtime/`.

1. Record the prompts on the Pi with Nova's server stopped. The recorder is `scripts/record_asr_corpus.py` in `edge/nova-hailo`; run it from that directory. It records 2 channels and the `--board` label is the condition. speech_v1 has 40 quiet and 10 noise prompts, and `--only` selects by that field, so: quiet takes the 40 quiet prompts, music takes the 10 noise prompts with laptop music at about 1 m, meeting takes the same 10 with laptop meeting audio. The recorder cannot select a different subset, so music and meeting have 10 prompts each.

   ```bash
   cd edge/nova-hailo
   python3 scripts/record_asr_corpus.py --corpus speech_v1 --board quiet --only quiet --channels 2
   python3 scripts/record_asr_corpus.py --corpus speech_v1 --board music --only noise --channels 2
   python3 scripts/record_asr_corpus.py --corpus speech_v1 --board meeting --only noise --channels 2
   ```

2. Record 3-minute negatives (no prompted speech) for quiet room, music, meeting, and "Nova speaking":

   ```bash
   arecord -D <xvf> -c2 -r16000 -f S16_LE -d 180 neg-quiet.wav   # likewise neg-music, neg-meeting, neg-nova
   ```

   Nova's own voice counts as a meeting-like negative: pass it as `meeting=`.
3. Optional third route, beam. On the Pi read the current setting with `xvf_host AUDIO_MGR_OP_R`, switch it to the processed/beam category, record the 10 noise prompts with `--board music-beam --only noise`, then restore with `xvf_host AUDIO_MGR_OP_R 7 3` (confirm it matches the value you read first). Never `SAVE_CONFIGURATION`.
4. Copy the recordings to the laptop and build one manifest per route (`manifest-ch0.json`, `manifest-ch1.json`). Route-suffixed boards such as `music-beam` are excluded by default; the beam route is built separately with `--channel ch0 --route-label beam --boards music-beam`:

   ```bash
   .venv/bin/python -m bench.voice.make_manifest --out runtime/voice-corpus/run1 \
     --corpus <speech_v1 dir> --boards quiet,music,meeting \
     --negative quiet=neg-quiet.wav --negative music=neg-music.wav \
     --negative meeting=neg-meeting.wav --negative meeting=neg-nova.wav
   ```

5. Replay each route twice against the real server. Before: start it with `AIBOX_VOICE_VAD=0` (no speech gate). After: default settings. Add `--realtime` for 20 ms pacing and `--api-key` (or `AIBOX_API_KEY`) when not on loopback.

   ```bash
   .venv/bin/python -m bench.voice.bench_voice --manifest runtime/voice-corpus/run1/manifest-ch1.json \
     --output runtime/results/ch1-before.json --realtime
   ```

   The "before" run (`AIBOX_VOICE_VAD=0`) reproduces the old server, but `bench_voice.py` bypasses the clients' energy gate (3x noise floor, minimum 350). False triggers on quiet negatives in "before" therefore overstate the old deployment; music and meeting would mostly have opened that gate anyway. Music and meeting have 10 prompts each, so they carry low statistical weight.

   Each result records the server VAD state per clip (`on`, `off` or `failed`). `score` prints it and marks the run invalid (overall FAIL) with a WARNING when a run mixes states or contains `failed`, because a failed VAD silently turns an "after" run into a "before" run.

6. Compare and gate:

   ```bash
   .venv/bin/python -m bench.voice.score --compare runtime/results/ch1-before.json runtime/results/ch1-after.json
   .venv/bin/python -m bench.voice.score runtime/results/ch0-after.json runtime/results/ch1-after.json
   ```

   | Gate | Threshold |
   |---|---|
   | WER, quiet | at most 10% |
   | WER, music | at most 20% |
   | False triggers, quiet and music negatives | at most 1 per 10 min |
   | `bytes_ok` | 100% |
   | Errored or timed-out clips | 0 |

   Meeting is reported only. The channel that passes every gate becomes the default: set `AIBOX_XVF_CHANNEL` (`0` or `1`, read by `aibox/audio/profile.py`) or change the default in `profile.py`.

Each result records the transcript, the `ignored` reason, `done.timings`, first-audio and total latency from `stop`, and per-sentence byte accounting (`bytes_ok`). A false trigger is a negative clip that was not ignored and returned words.

## Other live checks

Replay fixtures do not replace live testing. During playback, test a backchannel (`right`), a genuine interruption (`wait, wait`), side-talk to another person, and cancellation. Independent laptop meeting audio must be tested as its own scenario; it is not echo that the Pi can cancel.
