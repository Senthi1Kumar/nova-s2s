"""LiveKit agent adapter.

Optional subpackage: install with `uv sync --extra livekit`. Nothing here is
imported by the existing pipeline, the FastAPI app, or /v1/realtime, so the
demo path is unaffected whether or not the extra is present.

The Pi is the *agent*, not a thin client. An agent worker dials out to the
LiveKit SFU, which is what makes a roaming device reachable without an inbound
port or a static IP -- while wake word, VAD, Parakeet, Piper and the Hailo
fallback all stay on-device.

`bridge` deliberately imports no LiveKit packages, so the thread/asyncio
boundary can be tested without the extra installed.
"""
