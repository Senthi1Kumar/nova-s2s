"""Pipecat worker construction and ordered Studio WebSocket transport."""

from __future__ import annotations

from fastapi import WebSocket
from pipecat.frames.frames import InputAudioRawFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.transports.websocket.fastapi import (
    FastAPIWebsocketInputTransport,
    FastAPIWebsocketOutputTransport,
    FastAPIWebsocketParams,
    FastAPIWebsocketTransport,
)
from pipecat.workers.runner import WorkerRunner

from ..services import Services
from .audio import CHUNK_BYTES, SAMPLE_RATE
from .pipecat_services import VoiceResponseProcessor, VoiceTurnProcessor
from .protocol import VoiceProtocolSerializer
from .text import SentenceBuffer
from .turn_controller import TurnController
from .turn_judge import build_turn_judge

assert CHUNK_BYTES == 16000 * 2 * 4 // 100, "audio_out_10ms_chunks=4 at 16 kHz mono PCM16"

__all__ = ["SentenceBuffer", "run_voice_session"]


class OrderedAudioInputTransport(FastAPIWebsocketInputTransport):
    async def push_audio_frame(self, frame: InputAudioRawFrame):
        if self._params.audio_in_enabled and not self._paused:
            await self.push_frame(frame)


class ClientPacedWebsocketOutputTransport(FastAPIWebsocketOutputTransport):
    async def _write_audio_sleep(self):
        pass


async def run_voice_session(ws: WebSocket, services: Services) -> None:
    transport = FastAPIWebsocketTransport(
        ws,
        FastAPIWebsocketParams(
            serializer=VoiceProtocolSerializer(),
            audio_in_sample_rate=16000,
            audio_out_sample_rate=SAMPLE_RATE,
            audio_in_channels=1,
            audio_out_channels=1,
            audio_in_enabled=True,
            audio_in_passthrough=True,
            audio_out_enabled=True,
            audio_out_10ms_chunks=4,
            audio_out_end_silence_secs=0,
        ),
    )
    # Chunk-aligned padding leaves no remainder only at this rate; pipecat's
    # _bot_stopped_speaking() clears any leftover partial chunk.
    assert transport._params.audio_out_sample_rate == SAMPLE_RATE
    transport._input = OrderedAudioInputTransport(
        transport, transport._client, transport._params, name=transport._input_name
    )
    transport._output = ClientPacedWebsocketOutputTransport(
        transport, transport._client, transport._params, name=transport._output_name
    )
    pipeline = Pipeline([
        transport.input(),
        VoiceTurnProcessor(services, TurnController(build_turn_judge(services.settings))),
        VoiceResponseProcessor(services),
        transport.output(),
    ])
    worker = PipelineWorker(pipeline, enable_rtvi=False, params=PipelineParams(
        audio_in_sample_rate=16000,
        audio_out_sample_rate=16000,
    ))
    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(*_args):
        await worker.cancel(reason="Studio WebSocket disconnected")

    runner = WorkerRunner(handle_sigint=False, force_gc=False)
    await runner.add_workers(worker)
    await runner.run()
