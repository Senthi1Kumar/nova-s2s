# nova_hailo/cabingym/types.py
"""Router → executor contract for vehicle calls."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


@dataclass(frozen=True)
class VehicleCall:
    device: str
    api: str
    args: dict[str, Any] = field(default_factory=dict)
    grounding: dict[str, str] = field(default_factory=dict)
    score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"device": self.device, "api": self.api, "args": dict(self.args),
                "grounding": dict(self.grounding), "score": self.score}


@dataclass
class RouteOutcome:
    kind: Literal["vehicle", "clarify", "decline"]
    calls: list[VehicleCall] = field(default_factory=list)
    speak: str | None = None
    trace: dict[str, Any] = field(default_factory=dict)
