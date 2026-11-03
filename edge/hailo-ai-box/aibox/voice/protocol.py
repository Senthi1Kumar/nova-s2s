"""Versioned PCM16/JSON serialization for the Studio voice WebSocket.

Wire contract (server to client):

- Binary WebSocket messages are raw PCM16LE, mono, 16 kHz; never WAV.
- Binary data is sent only between `tts_start` and `tts_end` of one sentence.
  A nonzero `tts_start.bytes` is the exact total (Piper pads it to a multiple of
  1280); zero means unknown until `tts_end.bytes`, which is always authoritative
  for native streaming and includes Pipecat's zero-padding of the final chunk.
- Audio is append-only within a turn; sentences play back to back.
- A turn ends when `done` has arrived, every `tts_start` has its `tts_end`, and
  the playback buffer is drained.
- `ignored {turn_id, reason, speech_ms, ratio}` reports a turn dropped before
  ASR; `interrupted {turn_id}` reports a confirmed barge-in over the reply of
  that (old) turn, after which a `session` with the next turn_id follows.
- `session`, `transcript`, `ignored` and `done` carry `vad`: "on", "off" or
  "failed" (the server VAD broke and the turn ran ungated).
- Barge-in is opt-in (`AIBOX_VOICE_BARGE_IN`). While the reply plays, a client
  sends `start {barge_in:true}` plus audio; the server buffers it and confirms
  once it holds enough speech (250 ms by Silero, or 400 ms of audio with VAD
  off). An unconfirmed attempt is dropped without an event and the reply goes on.
- Clients drop binary data outside an open sentence of the current turn;
  `cancelled` clears all buffers.
"""

from __future__ import annotations

import json

from pipecat.frames.frames import (
    Frame,
    InputAudioRawFrame,
    InputTransportMessageFrame,
    OutputAudioRawFrame,
    OutputTransportMessageFrame,
    OutputTransportMessageUrgentFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer


class VoiceProtocolSerializer(FrameSerializer):
    async def serialize(self, frame: Frame) -> str | bytes | None:
        if isinstance(frame, OutputAudioRawFrame):
            return frame.audio
        if isinstance(frame, (OutputTransportMessageFrame, OutputTransportMessageUrgentFrame)):
            return json.dumps(frame.message, separators=(",", ":"))
        return None

    async def deserialize(self, data: str | bytes) -> Frame | None:
        if isinstance(data, bytes):
            return InputAudioRawFrame(audio=data, sample_rate=16000, num_channels=1)
        message = json.loads(data)
        if not isinstance(message, dict) or not isinstance(message.get("type"), str):
            return InputTransportMessageFrame(
                message={"type": "error", "error": {"code": "bad_message", "message": "expected JSON control"}}
            )
        return InputTransportMessageFrame(message=message)
