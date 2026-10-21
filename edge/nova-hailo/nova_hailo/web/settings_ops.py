"""nova.settings logic with no transport attached.

Callers: nova_hailo/web/realtime_session.py (/v1/realtime) and
nova_hailo/lk/control.py (LiveKit data channel). Both front doors speak the
same nova.* messages, so the Local Hailo <-> cloud switch, the integrations
status and the Google actions live here once. Voice-frontend settings (noise
gate, DTLN) stay in realtime_session: they act on that session's own mic path.
"""
from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def llm_snapshot(pipeline: Any) -> dict[str, Any]:
    """The pipeline's current LLM settings, or {} -- never raises."""
    fn = getattr(pipeline, "llm_settings_snapshot", None)
    if fn is None:
        return {}
    try:
        return fn() or {}
    except Exception:
        return {}


def settings_body(msg: dict[str, Any]) -> dict[str, Any]:
    """nova.settings.set carries its fields nested under "settings" or flat."""
    body = msg.get("settings")
    return body if isinstance(body, dict) else msg


def changes_llm(payload: dict[str, Any]) -> bool:
    return payload.get("mode") is not None or payload.get("or_model") is not None


_CLOUD_ORDER = ("openrouter", "groq", "cerebras")


def llm_settings(pipeline: Any, *, env: Mapping[str, str] = os.environ) -> dict[str, Any]:
    """The LLM choice a display shows and changes (GET /v1/settings, nova.settings ``llm``).

    ``provider`` is the configured one (a failover answer does not change it);
    ``ok`` says whether a provider can be picked: a key for clouds, the NPU for local.
    ``failover`` is the switch; ``failover_ready`` says a second provider stands by now.
    """
    from nova_hailo.backends.failover_llm import FailoverLLM
    from nova_hailo.backends.openrouter_llm import PROVIDERS
    from nova_hailo.settings_store import CLOUD_MODELS, LOCAL_MODELS

    backend = str(getattr(pipeline, "llm_backend", "") or "")
    llm = getattr(pipeline, "llm", None)
    primary = llm.primary if isinstance(llm, FailoverLLM) else llm
    cloud = backend in PROVIDERS
    providers = [
        {"id": name,
         "models": [m["id"] for m in CLOUD_MODELS.get(name, ())],
         "ok": any(str(env.get(k) or "").strip() for k in PROVIDERS[name][1])}
        for name in _CLOUD_ORDER
    ]
    providers.append({"id": "local", "models": [m["id"] for m in LOCAL_MODELS],
                      "ok": getattr(pipeline, "vdevice", None) is not None})
    return {
        "provider": backend if cloud else "local",
        "model": str(getattr(primary, "model", "") or "") if cloud
        else str(getattr(pipeline, "_local_hef_alias", "") or ""),
        "providers": providers,
        "failover": bool(getattr(pipeline, "_failover_enabled", True)),
        "failover_ready": isinstance(llm, FailoverLLM),
    }


async def apply_llm_choice(pipeline: Any, body: dict[str, Any]) -> tuple[bool, Any]:
    """POST /v1/settings ``{"llm": {"provider", "model"?}}`` → (True, settings) or (False, reason)."""
    from nova_hailo.backends.openrouter_llm import PROVIDERS

    choice = body.get("llm") if isinstance(body, dict) else None
    if not isinstance(choice, dict):
        return False, "missing_llm"
    provider = str(choice.get("provider") or "").strip().lower()
    model = choice.get("model")
    if provider == "local":
        kwargs: dict[str, Any] = {"mode": "local", "local_hef": model}
    elif provider in PROVIDERS:
        kwargs = {"mode": "openrouter", "provider": provider, "or_model": model}
    else:
        return False, "unknown_provider"
    if isinstance(choice.get("failover"), bool):
        kwargs["failover"] = choice["failover"]
    try:
        # A Hailo HEF load takes seconds; never on the event loop.
        await asyncio.to_thread(lambda: pipeline.apply_llm_settings(**kwargs))
    except Exception as exc:  # noqa: BLE001 — refused with the reason, settings unchanged
        return False, str(exc)
    return True, llm_settings(pipeline)


AVATARS: tuple[str, ...] = (
    "voice", "pebble", "capsule", "dome", "drop", "cloud", "gem", "visor", "eyes",
)


def tts_settings(pipeline: Any) -> dict[str, Any]:
    """The TTS choice a display shows: current voice and speed, the installed voices to pick from."""
    tts = getattr(pipeline, "tts", None)
    if not getattr(tts, "supports_voice_choice", False):
        return {"voice": "", "speed": "normal", "voices": []}
    from nova_hailo.backends.tts import PIPER_SPEEDS, list_piper_voices

    model_path = getattr(tts, "model_path", None)
    voices = list_piper_voices(Path(model_path).parent if model_path else None)
    # A change lands at the next turn; report what was chosen, not what is still playing.
    chosen = getattr(pipeline, "_hmi_settings", None) or {}
    voice = chosen.get("tts_voice")
    speed = chosen.get("tts_speed")
    return {
        "voice": voice if voice in {v["id"] for v in voices} else str(tts.voice_id),
        "speed": speed if speed in PIPER_SPEEDS else str(tts.speed),
        "voices": voices,
    }


def all_settings(pipeline: Any) -> dict[str, Any]:
    return {
        "llm": llm_settings(pipeline),
        "tts": tts_settings(pipeline),
        "avatar": str(getattr(pipeline, "avatar", "voice")),
    }


async def apply_tts_choice(pipeline: Any, choice: Any) -> tuple[bool, str | None]:
    """``{"voice"?, "speed"?}`` → (True, None) or (False, reason); nothing applies on a refusal."""
    from nova_hailo.backends.tts import PIPER_SPEEDS

    if not isinstance(choice, dict) or not ({"voice", "speed"} & choice.keys()):
        return False, "bad_tts"
    if not getattr(getattr(pipeline, "tts", None), "supports_voice_choice", False):
        return False, "no_voice_choice"
    voice, speed = choice.get("voice"), choice.get("speed")
    if voice is not None and voice not in {v["id"] for v in tts_settings(pipeline)["voices"]}:
        return False, "unknown_voice"
    if speed is not None and speed not in PIPER_SPEEDS:
        return False, "unknown_speed"
    try:
        # A voice load reads an ONNX model; never on the event loop.
        await asyncio.to_thread(lambda: pipeline.apply_tts_settings(voice=voice, speed=speed))
    except ValueError as exc:
        return False, str(exc)
    except Exception:  # noqa: BLE001 -- a corrupt or truncated model; the other parts still apply
        logger.exception("TTS voice load failed")
        return False, "voice_load_failed"
    return True, None


_QUIET_STATES = ("IDLE", "ARMED", "LISTENING")


def turn_in_flight() -> bool:
    """A reply is being produced or spoken: the voice session is past listening."""
    from nova_hailo.web.session_control import CONTROL

    try:
        return str(CONTROL.status().get("state") or "IDLE") not in _QUIET_STATES
    except Exception:  # noqa: BLE001 -- no signal means no session to protect
        return False


PREVIEW_LINE = "Hi, I'm Nova. This is how I'll sound."


async def preview_tts(pipeline: Any, choice: Any) -> dict[str, Any]:
    """``{"voice"?, "speed"?}`` → speak PREVIEW_LINE that way, saving and changing nothing."""
    from nova_hailo.backends.tts import PIPER_SPEEDS

    tts = getattr(pipeline, "tts", None)
    if not getattr(tts, "supports_voice_choice", False):
        return {"ok": False, "error": "no_voice_choice"}
    choice = choice if isinstance(choice, dict) else {}
    voice = choice.get("voice") or str(tts.voice_id)
    speed = choice.get("speed") or str(tts.speed)
    if voice not in {v["id"] for v in tts_settings(pipeline)["voices"]}:
        return {"ok": False, "error": "unknown_voice"}
    if speed not in PIPER_SPEEDS:
        return {"ok": False, "error": "unknown_speed"}
    if tts.is_speaking or turn_in_flight():
        return {"ok": False, "error": "speaking"}
    try:
        # A voice load reads an ONNX model; never on the event loop.
        started = await asyncio.to_thread(lambda: tts.preview(PREVIEW_LINE, voice, speed))
        if started is False:
            if not getattr(tts, "ready", True):
                return {"ok": False, "error": "not_ready"}
            return {"ok": False, "error": "speaking"}   # a turn began while the voice loaded
    except Exception:  # noqa: BLE001 -- a corrupt or truncated model
        logger.exception("TTS preview failed")
        return {"ok": False, "error": "voice_load_failed"}
    return {"ok": True}


def apply_avatar_choice(pipeline: Any, value: Any) -> tuple[bool, str | None]:
    if not isinstance(value, str) or value not in AVATARS:
        return False, "unknown_avatar"
    pipeline.set_avatar(value)
    return True, None


async def apply_settings(pipeline: Any, body: dict[str, Any]) -> tuple[bool, dict[str, str]]:
    """Apply each of ``llm``, ``tts``, ``avatar`` on its own → (anything applied, {part: reason})."""
    errors: dict[str, str] = {}
    applied = False
    if "llm" in body:
        ok, out = await apply_llm_choice(pipeline, body)
        applied |= ok
        if not ok:
            errors["llm"] = str(out)
    if "tts" in body:
        ok, reason = await apply_tts_choice(pipeline, body["tts"])
        applied |= ok
        if not ok:
            errors["tts"] = str(reason)
    if "avatar" in body:
        ok, reason = apply_avatar_choice(pipeline, body["avatar"])
        applied |= ok
        if not ok:
            errors["avatar"] = str(reason)
    if not body.keys() & {"llm", "tts", "avatar"}:
        errors["llm"] = "missing_llm"
    return applied, errors


async def apply_llm_settings(
    pipeline: Any, payload: dict[str, Any]
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Switch the LLM off the event loop -- a HEF load takes seconds.

    Returns (new LLM snapshot or None on failure, the nova.llm_status message).
    """
    apply = getattr(pipeline, "apply_llm_settings", None)
    if apply is None:
        return None, {
            "type": "nova.llm_status",
            "status": "error",
            "error": "llm settings not supported",
        }
    try:
        snap = await asyncio.to_thread(
            lambda: apply(
                mode=payload.get("mode"),
                local_hef=payload.get("local_hef"),
                or_model=payload.get("or_model"),
            )
        )
    except Exception as exc:  # noqa: BLE001
        return None, {"type": "nova.llm_status", "status": "error", "error": str(exc)}
    return snap, {"type": "nova.llm_status", "status": "ready"}


def integrations_snapshot() -> dict[str, Any]:
    """Read-only Google + connector status for the QML driver face.

    The QML driver face only ever shows this, never configures it (that stays
    in the browser settings UI), so a stale or missing value must never block
    a turn. Each lookup is independently wrapped: a failure in one must not
    take out the other, and any failure falls back to a disconnected/empty
    state rather than raising.
    """
    google = {"connected": False, "needs_reauth": False}
    try:
        from nova_hailo.google_oauth import integration_status

        status = integration_status() or {}
        needs_reauth = bool(status.get("needs_reauth"))
        google = {
            "connected": bool(status.get("authenticated")) and not needs_reauth,
            "needs_reauth": needs_reauth,
        }
    except Exception:
        pass

    connectors = {"enabled": 0, "tools": 0}
    try:
        from nova_hailo.connectors.registry import list_connectors, total_enabled_tools

        enabled = sum(1 for c in (list_connectors() or []) if c.get("enabled"))
        connectors = {"enabled": enabled, "tools": int(total_enabled_tools() or 0)}
    except Exception:
        pass

    return {"google": google, "connectors": connectors}


def google_connect_payload() -> dict[str, Any]:
    """Kick off (or resume) the Google OAuth flow for a websocket client.

    Read-only from the QML driver face's perspective: it only ever receives
    this reply and opens ``auth_url`` in a real browser -- an OAuth consent
    screen cannot render usefully on the driver display. Mirrors the REST
    ``/integrations/google/auth/start`` handler in web/app.py so both
    surfaces share one behavior. Any failure to start the flow degrades to
    an error reply instead of raising into the WS reader loop.
    """
    from nova_hailo.google_oauth import begin_ui_oauth_flow

    try:
        result = begin_ui_oauth_flow() or {}
    except Exception as exc:  # noqa: BLE001
        return {"type": "nova.google.auth_url", "auth_url": None, "error": str(exc)}
    return {
        "type": "nova.google.auth_url",
        "auth_url": result.get("auth_url"),
        "status": result.get("status"),
    }


def google_disconnect_payload() -> dict[str, Any]:
    """Forget stored refresh tokens locally (does not revoke at Google).

    Mirrors the REST ``/integrations/google/disconnect`` handler. Never
    raises: a corrupt or unreadable token store just means there was nothing
    to forget.
    """
    from nova_hailo.google_oauth import GoogleTokenProvider

    try:
        GoogleTokenProvider().store.clear()
    except Exception:
        pass
    return {"type": "nova.google.disconnected"}
