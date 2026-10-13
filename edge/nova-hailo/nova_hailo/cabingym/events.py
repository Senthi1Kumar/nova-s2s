# nova_hailo/cabingym/events.py
"""Realtime event payloads for vehicle state. Callers: nova_hailo.web.realtime_session."""
from __future__ import annotations

from typing import Any

from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.cabingym.vss import Change


def vehicle_snapshot_event(cabin: CabinEnv) -> dict[str, Any]:
    snap = cabin.store.snapshot(with_source=True)
    return {
        "type": "nova.vehicle", "seq": 0, "profile": cabin.profile.name,
        "adapter": cabin.adapter.name,
        "snapshot": {path: {"value": value, "source": source} for path, (value, source) in snap.items()},
    }


def vehicle_delta_event(changes: list[Change], seq: int) -> dict[str, Any]:
    return {"type": "nova.vehicle", "seq": seq, "changed": {ch.path: ch.after for ch in changes}}
