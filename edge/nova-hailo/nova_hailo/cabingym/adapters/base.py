# nova_hailo/cabingym/adapters/base.py
from __future__ import annotations

from typing import Protocol

from nova_hailo.cabingym.vss import Change, VssStore


class VehicleAdapter(Protocol):
    """Fulfils actuator targets and feeds sensor values into a VssStore.

    InProcessSim is the only implementation in slice 1; KUKSA, GPS-NMEA and
    OBD-II read adapters implement the same protocol later.
    """

    name: str

    def attach(self, store: VssStore) -> None: ...

    def tick(self, dt_s: float) -> list[Change]: ...

    def settle(self) -> list[Change]: ...
