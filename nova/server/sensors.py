"""Hardware-neutral sensor boundary for DriveAuth evidence.

Mock demo fabricates audio only inside ``nova.server.driveauth_bridge`` when
``DRIVEAUTH_USE_MOCK=1``. Production providers must supply real samples; Nova
never synthesizes biometric evidence outside mock mode.

Later hardware work swaps providers without changing routing, authorization
state, or tool execution.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class VoiceSensor(Protocol):
    def capture(self, seconds: float = 1.5) -> np.ndarray | None: ...


@runtime_checkable
class FaceSensor(Protocol):
    def capture(self) -> np.ndarray | None: ...


@runtime_checkable
class FingerprintSensor(Protocol):
    def capture(self) -> np.ndarray | None: ...


@runtime_checkable
class VehicleContextSensor(Protocol):
    def snapshot(self) -> dict[str, float | bool | str]: ...


# Sensitive tools that may later require DriveAuth. Only send_payment is gated today.
SENSITIVE_TOOL_POLICY: dict[str, str] = {
    "send_payment": "payment",
    # Future (do not silently gate until UX/risk tiers are specified):
    # "send_email": "standard",
    # "delete_calendar_event": "standard",
    # "create_drive_folder": "micro",
}
