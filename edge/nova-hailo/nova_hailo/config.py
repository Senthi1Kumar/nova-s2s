from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parent.parent

# .env is loaded in nova_hailo/__init__.py (runs for every entrypoint).
DEFAULT_CONFIG = ROOT / "config.yaml"
SOUL_PATH = ROOT / "prompts" / "soul.md"
DOWNLOADS = Path.home() / "Downloads"

# Callers: AppConfig.load, web/app.py, pipeline, scripts.
PROFILE_CONFIGS = {
    "oem": ROOT / "config.oem.yaml",
    "oem_readonly": ROOT / "config.oem.yaml",
    "oem_rollback": ROOT / "config.oem_rollback.yaml",
    "default": DEFAULT_CONFIG,
}


def resolve_config_path(path: str | Path | None = None) -> Path:
    """Resolve config from explicit path, NOVA_HAILO_CONFIG, or NOVA_HAILO_PROFILE."""
    if path:
        return Path(path)
    env_cfg = (os.environ.get("NOVA_HAILO_CONFIG") or "").strip()
    if env_cfg:
        return Path(env_cfg)
    profile = (os.environ.get("NOVA_HAILO_PROFILE") or "").strip().lower()
    if profile in PROFILE_CONFIGS:
        return PROFILE_CONFIGS[profile]
    return DEFAULT_CONFIG

# Prefer models/ then ~/Downloads
LLAMA32_CANDIDATES = [
    ROOT / "models" / "Llama3.2-1B-Instruct.hef",
    DOWNLOADS / "Llama3.2-1B-Instruct.hef",
]
QWEN2_CANDIDATES = [
    ROOT / "models" / "Qwen2-1.5B-Instruct.hef",
    DOWNLOADS / "Qwen2-1.5B-Instruct.hef",
]
QWEN25_CANDIDATES = [
    ROOT / "models" / "Qwen2.5-1.5B-Instruct.hef",
    DOWNLOADS / "Qwen2.5-1.5B-Instruct.hef",
    Path("/usr/local/hailo/resources/models/hailo10h/Qwen2.5-1.5B-Instruct.hef"),
]
QWEN3_CANDIDATES = [
    ROOT / "models" / "Qwen3-1.7B-Instruct.hef",
    DOWNLOADS / "Qwen3-1.7B-Instruct.hef",
    Path("/usr/local/hailo/resources/models/hailo10h/Qwen3-1.7B-Instruct.hef"),
]
QWEN2_FC_CANDIDATES = [
    ROOT / "models" / "Qwen2-1.5B-Instruct-Function-Calling-v1.hef",
    DOWNLOADS / "Qwen2-1.5B-Instruct-Function-Calling-v1.hef",
]
DEEPSEEK_HEF_CANDIDATES = [
    ROOT / "models" / "DeepSeek-R1-Distill-Qwen-1.5B.hef",
    DOWNLOADS / "DeepSeek-R1-Distill-Qwen-1.5B.hef",
    Path("/usr/local/hailo/resources/models/hailo10h/DeepSeek-R1-Distill-Qwen-1.5B.hef"),
]
CODER_HEF = Path("/usr/local/hailo/resources/models/hailo10h/Qwen2.5-Coder-1.5B-Instruct.hef")
MIN_HEF_BYTES = 100_000_000


@dataclass
class AppConfig:
    raw: dict = field(default_factory=dict)
    path: Path = DEFAULT_CONFIG

    @classmethod
    def load(cls, path: str | Path | None = None) -> AppConfig:
        p = resolve_config_path(path)
        data: dict[str, Any] = {}
        if p.exists():
            with open(p) as f:
                data = yaml.safe_load(f) or {}
        return cls(raw=data, path=p)

    def get(self, *keys, default=None):
        cur: Any = self.raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    @property
    def soul_prompt(self) -> str:
        if SOUL_PATH.exists():
            return SOUL_PATH.read_text().strip()
        return "You are a helpful voice assistant. Be concise."


def _valid_hef(p: Path) -> bool:
    try:
        return p.is_file() and p.stat().st_size >= MIN_HEF_BYTES
    except OSError:
        return False


def _first_valid(candidates: list[Path]) -> str | None:
    for c in candidates:
        if _valid_hef(c):
            return str(c.resolve())
    return None


def resolve_llm_hef(hef_path: str | None = None) -> str:
    if hef_path:
        key = hef_path.strip().lower()
        alias_map = {
            "llama32": LLAMA32_CANDIDATES,
            "llama3.2": LLAMA32_CANDIDATES,
            "llama": LLAMA32_CANDIDATES,
            "qwen2": QWEN2_CANDIDATES,
            "qwen": QWEN2_CANDIDATES,
            "qwen25": QWEN25_CANDIDATES,
            "qwen2.5": QWEN25_CANDIDATES,
            "qwen3": QWEN3_CANDIDATES,
            "qwen3-1.7b": QWEN3_CANDIDATES,
            "qwen2-fc": QWEN2_FC_CANDIDATES,
            "function": QWEN2_FC_CANDIDATES,
            "deepseek": DEEPSEEK_HEF_CANDIDATES,
            "r1": DEEPSEEK_HEF_CANDIDATES,
            "deepseek-r1": DEEPSEEK_HEF_CANDIDATES,
            "coder": [CODER_HEF],
            "qwen-coder": [CODER_HEF],
        }
        if key in alias_map:
            found = _first_valid(alias_map[key])
            if found:
                return found
            raise FileNotFoundError(
                f"llm_hef={hef_path!r} is a known alias but no matching HEF exists. "
                f"Looked for: {', '.join(str(c) for c in alias_map[key])}"
            )
        p = Path(hef_path).expanduser()
        if p.exists() and _valid_hef(p):
            return str(p.resolve())
        # Falling through to the default group silently loads Qwen2 for any typo,
        # which is indistinguishable from "my config edit did nothing".
        raise FileNotFoundError(
            f"llm_hef={hef_path!r} is neither a known alias nor an existing HEF. "
            f"Known aliases: {', '.join(sorted(alias_map))}"
        )

    for group in (QWEN2_CANDIDATES, LLAMA32_CANDIDATES, DEEPSEEK_HEF_CANDIDATES, [CODER_HEF]):
        found = _first_valid(group)
        if found:
            return found

    raise FileNotFoundError(
        "No LLM HEF found. Put Llama3.2-1B-Instruct.hef in models/ or ~/Downloads "
        "(or pass --llm-hef llama32|qwen2|path)."
    )


def resolve_audio_device_ids(
    input_name: str | None = None,
    output_name: str | None = None,
) -> tuple[int | None, int | None]:
    try:
        import sounddevice as sd
    except ImportError:
        return None, None

    devices = sd.query_devices()
    pref = (input_name or "wm8960").lower()
    pref_out = (output_name or input_name or "wm8960").lower()
    if pref in {"default", "auto", ""}:
        pref = "wm8960"
    if pref_out in {"default", "auto", ""}:
        pref_out = "wm8960"

    def find(kind: str, needle: str) -> int | None:
        needle = needle.lower()
        candidates = []
        for i, d in enumerate(devices):
            name = str(d.get("name", "")).lower()
            max_in = int(d.get("max_input_channels", 0) or 0)
            max_out = int(d.get("max_output_channels", 0) or 0)
            if kind == "input" and max_in <= 0:
                continue
            if kind == "output" and max_out <= 0:
                continue
            score = 0
            # Waveshare WM8960 ships an asound.conf that puts the card behind
            # dmix ("playback"/"dmixed") and dsnoop ("capture"/"array"). Opening
            # the raw hw device instead collides with dmix and PortAudio returns
            # -9985 Device unavailable — measured 2026-07-29. Always prefer the
            # plug PCMs: they also resample (Piper is 16k, card runs 44.1/48k).
            plug_out = {"playback": 240, "dmixed": 230, "default": 200}
            plug_in = {"capture": 240, "array": 230, "default": 200}
            preferred = plug_out if kind == "output" else plug_in
            score += preferred.get(name.strip(), 0)
            if needle in name:
                score += 100
            if "wm8960" in name:
                score += 50
            if "hdmi" in name or "vc4" in name:
                score -= 80
            if score > 0:
                candidates.append((score, i, name))
        if not candidates:
            return None
        candidates.sort(key=lambda x: (-x[0], x[1]))
        return candidates[0][1]

    return find("input", pref), find("output", pref_out)


def list_audio_devices() -> str:
    try:
        import sounddevice as sd
    except ImportError:
        return "sounddevice not installed"
    lines = ["sounddevice devices:"]
    for i, d in enumerate(sd.query_devices()):
        lines.append(
            f"  [{i}] {d['name']}  in={d['max_input_channels']} out={d['max_output_channels']} "
            f"sr={d.get('default_samplerate')}"
        )
    inn, out = resolve_audio_device_ids("wm8960", "wm8960")
    lines.append(f"preferred wm8960: input={inn} output={out}")
    return "\n".join(lines)
