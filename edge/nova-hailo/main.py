#!/usr/bin/env python3
"""Nova-Hailo CLI — cascaded voice/text agent for Hailo-10H with full metrics."""
from __future__ import annotations

import argparse
import json
import sys
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

NSK_ROOT = Path(__file__).resolve().parent
HAILO_APPS = Path("/home/cariad/code/hailo-apps")
for p in (str(NSK_ROOT), str(HAILO_APPS)):
    if p not in sys.path:
        sys.path.insert(0, p)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Nova-Hailo: cascaded voice agent on Hailo-10H",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 main.py --list-audio
  python3 main.py --no-tts text
  python3 main.py --llm-hef qwen2 text
  python3 main.py --audio-device wm8960 voice
  python3 main.py voice --vad
        """,
    )
    p.add_argument("--config", type=str, default=None)
    p.add_argument("--llm-hef", type=str, default=None, help="HEF path or alias: deepseek|coder")
    p.add_argument("--whisper-hef", type=str, default=None)
    p.add_argument("--no-tts", action="store_true")
    p.add_argument("--debug", action="store_true")
    p.add_argument("--audio-device", type=str, default=None, help="e.g. wm8960 (input+output)")
    p.add_argument("--audio-in", type=str, default=None)
    p.add_argument("--audio-out", type=str, default=None)
    p.add_argument("--list-audio", action="store_true")
    p.add_argument("--metrics-json", type=str, default=None, help="Append turn metrics JSONL")
    p.add_argument("--no-think", action="store_true", help="Force no_think ON")
    p.add_argument("--think", action="store_true", help="Allow R1 thinking (no_think OFF)")
    p.add_argument("--profile", action="store_true", help="Hailo profile notes + HAILO_MONITOR=1")
    p.add_argument("--tools", action="store_true", help="Put tool schemas in system prompt")
    p.add_argument(
        "--resident-stt",
        action="store_true",
        help="Keep Whisper loaded (sequential_stt=false; cuts ~1s/turn load)",
    )
    p.add_argument(
        "--sequential-stt",
        action="store_true",
        help="Force load→infer→release Whisper each turn",
    )

    sub = p.add_subparsers(dest="mode")
    sub.add_parser("text", help="Interactive text agent")
    voice = sub.add_parser("voice", help="Mic STT → agent → TTS")
    voice.add_argument("--vad", action="store_true")
    return p


def _print_metrics(result, metrics_json: str | None):
    print(result.metrics.pretty())
    if metrics_json:
        with open(metrics_json, "a") as f:
            f.write(json.dumps(result.metrics.to_dict()) + "\n")


def run_text(pipeline, metrics_json=None):
    print("Nova-Hailo text mode. Type a message, 'clear', 'stats', or 'quit'.\n")
    while True:
        try:
            line = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line.lower() in {"q", "quit", "exit"}:
            break
        if line.lower() in {"c", "clear"}:
            pipeline.clear_context()
            continue
        if line.lower() in {"stats", "summary"}:
            print(pipeline.session.summary())
            continue
        pipeline.abort_event.clear()
        result = pipeline.run_text_turn(line)
        _print_metrics(result, metrics_json)
    print("session:", pipeline.session.summary())


def run_voice(pipeline, args, cfg, metrics_json=None):
    from nova_hailo.voice_loop import SimpleVoiceLoop

    def on_audio_ready(audio):
        pipeline.abort_event.clear()
        result = pipeline.run_audio_turn(audio)
        _print_metrics(result, metrics_json)

    def on_processing_start():
        pipeline.on_abort()
        if pipeline.tts:
            pipeline.tts.interrupt()

    print(f"Audio input device id: {pipeline.audio_in_id}")
    print(f"Audio output preferred id: {pipeline.audio_out_id}")

    loop = SimpleVoiceLoop(
        on_audio=on_audio_ready,
        on_start=on_processing_start,
        on_clear=pipeline.clear_context,
        on_abort=pipeline.on_abort,
        on_shutdown=None,
        device_id=pipeline.audio_in_id,
        title="Nova-Hailo",
    )
    loop.run()
    print("session:", pipeline.session.summary())



def main():
    parser = build_parser()
    args = parser.parse_args()

    from nova_hailo.config import AppConfig, list_audio_devices

    if args.list_audio:
        print(list_audio_devices())
        return

    if args.profile:
        from nova_hailo.hailo_profile import (
            enable_hailo_monitor_env,
            print_monitor_instructions,
            try_fw_identify,
        )

        note = enable_hailo_monitor_env()
        print_monitor_instructions()
        print("FW:\n", try_fw_identify())
        print("note:", note)

    if not args.mode:
        parser.print_help()
        return

    from nova_hailo.pipeline import NovaPipeline

    cfg = AppConfig.load(args.config)
    # CLI overrides
    if args.think:
        cfg.raw.setdefault("model", {})["no_think"] = False
        cfg.raw.setdefault("llm", {})["no_think"] = False
    if args.no_think:
        cfg.raw.setdefault("model", {})["no_think"] = True
        cfg.raw.setdefault("llm", {})["no_think"] = True
    if args.tools:
        cfg.raw.setdefault("tools", {})["enable_in_prompt"] = True
    if args.resident_stt:
        cfg.raw.setdefault("pipeline", {})["sequential_stt"] = False
    if args.sequential_stt:
        cfg.raw.setdefault("pipeline", {})["sequential_stt"] = True
    if args.profile:
        from nova_hailo.hailo_profile import enable_hailo_monitor_env

        enable_hailo_monitor_env()

    text_only = args.mode == "text"
    audio = args.audio_device
    audio_in = args.audio_in or audio or cfg.get("voice", "input_device")
    audio_out = args.audio_out or audio or cfg.get("voice", "output_device")

    print("Initializing Nova-Hailo (Hailo-10H)...")
    with redirect_stderr(StringIO()):
        pipeline = NovaPipeline(
            cfg=cfg,
            llm_hef=args.llm_hef,
            whisper_hef=args.whisper_hef,
            no_tts=args.no_tts,
            text_only=text_only,
            audio_in=audio_in,
            audio_out=audio_out,
        )
    print(f"LLM: {pipeline.llm.hef_path}")
    if pipeline.stt:
        print(f"STT (resident): {pipeline.stt.hef_path}")
    elif not text_only:
        print(f"STT: on-demand ({pipeline._whisper_hef}) [sequential_stt]")
    print(f"TTS: {'on' if pipeline.tts.enabled else 'off'}", end="")
    if pipeline.tts.enabled and pipeline.tts.model_path:
        print(f" ({pipeline.tts.model_path})")
    else:
        print()
    print(f"Tools: {', '.join(pipeline.tools.keys())}")
    print(f"Audio in/out ids: {pipeline.audio_in_id} / {pipeline.audio_out_id}")
    print()

    try:
        if args.mode == "text":
            run_text(pipeline, args.metrics_json)
        else:
            run_voice(pipeline, args, cfg, args.metrics_json)
    finally:
        pipeline.close()
        print("Shutdown complete.")


if __name__ == "__main__":
    main()
