# nova_hailo/cabingym/devices/media.py
from __future__ import annotations

from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.devices.base import (
    Api, ApiContext, ApiResult, clamp, device, done, p_delta, p_enum, p_span, p_value,
    resolve_value,
)

SOURCES = ("radio", "bluetooth", "usb", "streaming")
SOURCE_LABEL = {"radio": "the radio", "bluetooth": "Bluetooth", "usb": "USB", "streaming": "streaming"}
SOURCE_SYNONYMS = {"radio": ("fm", "am"), "bluetooth": ("my phone",),
                   "streaming": ("spotify", "apple music", "youtube music")}


def _play(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    query = args["query"]
    targets: dict[str, Any] = {c.MEDIA_STATE: "playing", c.MEDIA_TITLE: query, c.MEDIA_TRACK: 0}
    if args.get("source"):
        targets[c.MEDIA_SOURCE] = args["source"]
    return done(f"Playing {query}.", targets)


def _pause(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    if ctx.snapshot[c.MEDIA_STATE] != "playing":
        return done("Nothing is playing.")
    return done("Paused.", {c.MEDIA_STATE: "paused"})


def _resume(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    if ctx.snapshot[c.MEDIA_STATE] == "playing":
        return done("It's already playing.")
    return done("Resuming.", {c.MEDIA_STATE: "playing"})


def _skip(step: int):
    def handler(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
        track = max(0, int(ctx.snapshot[c.MEDIA_TRACK]) + step)
        return done("Next track." if step > 0 else "Previous track.",
                    {c.MEDIA_TRACK: track, c.MEDIA_STATE: "playing", c.MEDIA_TITLE: ""})
    return handler


def _volume(ctx: ApiContext, args: dict[str, Any]) -> ApiResult:
    level = int(clamp(resolve_value(args, ctx.snapshot[c.VOLUME]), 0, 100))
    return done("Muted." if level == 0 else f"Volume {level}.", {c.VOLUME: level})


DEVICE = device(
    "media", "music, radio, volume and audio source",
    r"\b(music|songs?|tracks?|play\w*|pause|volume|radio|spotify|audio|skip|louder|quieter)\b",
    [
        Api("pause_media", "pause the music", _pause, writes=(c.MEDIA_STATE,),
            patterns=(r"\b(pause|stop)\s+(the\s+)?(music|song|track|playback|audio|radio|podcast)\b|"
                      r"^\s*pause\b",)),
        Api("resume_media", "resume the music", _resume, writes=(c.MEDIA_STATE,),
            patterns=(r"\b(resume|unpause|continue)\b|^\s*play\s*(the\s+)?(music)?\s*[.!?]*$",)),
        Api("next_track", "skip to the next song", _skip(1), writes=(c.MEDIA_TRACK, c.MEDIA_STATE),
            patterns=(r"\b(next|skip)\s+(this\s+|the\s+)?(song|track|one)\b|"
                      r"^\s*(skip|next)(\s+(it|this))?\s*[.!?]*$|\bskip\s+(this|it)\b",)),
        Api("previous_track", "go back to the previous song", _skip(-1),
            writes=(c.MEDIA_TRACK, c.MEDIA_STATE),
            patterns=(r"\b(previous|last)\s+(song|track)\b|\bgo\s+back\s+a\s+(song|track)\b",)),
        Api("set_volume", "set or change the volume", _volume,
            params={"value": p_value("percent", "Volume percent", 0, 100),
                    "delta": p_delta("percent", "Change in volume")},
            requires_any=("value", "delta"), writes=(c.VOLUME,),
            patterns=(r"\b(volume|louder|quieter|softer|turn\s+(it|the\s+music)\s+(up|down)|"
                      r"mute\s+(the\s+)?(music|audio|sound|radio)|too\s+loud)\b",)),
        Api("set_source", "switch the audio source", lambda ctx, args: done(
                f"Switched to {SOURCE_LABEL[args['source']]}.", {c.MEDIA_SOURCE: args["source"]}),
            params={"source": p_enum(SOURCES, desc="Audio source", synonyms=SOURCE_SYNONYMS)},
            required=("source",), writes=(c.MEDIA_SOURCE,),
            patterns=(r"\b(switch|change)\s+(to|the\s+source\s+to)\s+(the\s+)?"
                      r"(radio|fm|am|bluetooth|usb|spotify|streaming)\b|"
                      r"\b(put|turn)\s+on\s+the\s+radio\b|\bsource\b",)),
        Api("play_media", "play a song, artist, genre or station", _play,
            params={"query": p_span("media", "What to play"),
                    "source": p_enum(SOURCES, desc="Audio source", synonyms=SOURCE_SYNONYMS)},
            required=("query",), writes=(c.MEDIA_STATE, c.MEDIA_TITLE, c.MEDIA_TRACK, c.MEDIA_SOURCE),
            patterns=(r"\bplay\b",)),
    ],
)
