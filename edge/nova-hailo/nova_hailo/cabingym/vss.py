# nova_hailo/cabingym/vss.py
"""VSS-path vehicle state store.

Callers: cabingym.catalog (build_store), .adapters, .executor, .env.
Values are the *current* state. Actuator writes go through set_targets(),
which asks the attached target sink (an adapter) for the new current value.
Every value remembers the source that last wrote it so real signals
(obd/gps/kuksa) are never overwritten by the simulator.
"""
from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

Kind = Literal["sensor", "actuator", "attribute"]
DType = Literal["float", "int", "bool", "str", "enum", "list"]
REAL_SOURCES = frozenset({"obd", "gps", "kuksa"})
SOURCES = frozenset({"sim"}) | REAL_SOURCES


class SignalError(ValueError):
    """Unknown path, wrong kind, or a value outside the signal's type/range/enum."""


@dataclass(frozen=True)
class Signal:
    path: str
    kind: Kind
    dtype: DType
    unit: str | None = None
    min: float | None = None
    max: float | None = None
    allowed: tuple[str, ...] | None = None
    default: Any = None
    description: str = ""
    profiles: frozenset[str] = frozenset({"ev", "ice"})
    scored: bool = True


@dataclass(frozen=True)
class Change:
    path: str
    before: Any
    after: Any

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "before": self.before, "after": self.after}


def coerce(sig: Signal, value: Any) -> Any:
    """Validate ``value`` against ``sig`` and return the stored form."""
    if sig.dtype == "bool":
        if isinstance(value, bool):
            return value
        raise SignalError(f"{sig.path}: expected bool, got {value!r}")
    if sig.dtype in ("float", "int"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SignalError(f"{sig.path}: expected number, got {value!r}")
        if isinstance(value, float) and not math.isfinite(value):
            raise SignalError(f"{sig.path}: non-finite value")
        if sig.dtype == "int":
            if float(value) != int(value):
                raise SignalError(f"{sig.path}: expected integer, got {value!r}")
            out: Any = int(value)
        else:
            out = float(value)
        if sig.min is not None and out < sig.min:
            raise SignalError(f"{sig.path}: {out} below {sig.min}")
        if sig.max is not None and out > sig.max:
            raise SignalError(f"{sig.path}: {out} above {sig.max}")
        return out
    if sig.dtype == "str":
        if isinstance(value, str):
            return value
        raise SignalError(f"{sig.path}: expected str, got {value!r}")
    if sig.dtype == "enum":
        if isinstance(value, str) and sig.allowed and value in sig.allowed:
            return value
        raise SignalError(f"{sig.path}: {value!r} not in {sig.allowed}")
    if sig.dtype == "list":
        if isinstance(value, (list, tuple)):
            return tuple(value)
        raise SignalError(f"{sig.path}: expected list, got {value!r}")
    raise SignalError(f"{sig.path}: unknown dtype {sig.dtype}")


TargetSink = Callable[[str, Any], Any]
Listener = Callable[[list[Change]], None]


class VssStore:
    def __init__(self, signals: Iterable[Signal]):
        self._signals: dict[str, Signal] = {}
        for sig in signals:
            if sig.path in self._signals:
                raise SignalError(f"duplicate signal {sig.path}")
            self._signals[sig.path] = sig
        self._values: dict[str, Any] = {
            p: (None if s.default is None else coerce(s, s.default))
            for p, s in self._signals.items()
        }
        self._targets: dict[str, Any] = {}
        self._sources: dict[str, str] = dict.fromkeys(self._signals, "sim")
        self._lock = threading.RLock()
        self._listeners: list[Listener] = []
        self.target_sink: TargetSink | None = None

    def signal(self, path: str) -> Signal:
        try:
            return self._signals[path]
        except KeyError:
            raise SignalError(f"unknown signal {path}") from None

    def paths(self) -> list[str]:
        return sorted(self._signals)

    def has(self, path: str) -> bool:
        return path in self._signals

    def get(self, path: str) -> Any:
        self.signal(path)
        with self._lock:
            return self._values[path]

    def target(self, path: str) -> Any:
        with self._lock:
            return self._targets.get(path)

    def source(self, path: str) -> str:
        self.signal(path)
        with self._lock:
            return self._sources[path]

    def validate_targets(self, items: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for path, value in items.items():
            sig = self.signal(path)
            if sig.kind != "actuator":
                raise SignalError(f"{path}: {sig.kind} is not writable")
            out[path] = coerce(sig, value)
        return out

    def set_targets(self, items: Mapping[str, Any]) -> list[Change]:
        coerced = self.validate_targets(items)
        with self._lock:
            before = dict(self._values)
            for path, value in coerced.items():
                self._targets[path] = value
                if self._sources[path] in REAL_SOURCES:
                    continue
                sink = self.target_sink
                current = value if sink is None else sink(path, value)
                if current is not None:
                    self._values[path] = coerce(self._signals[path], current)
                    self._sources[path] = "sim"
            changes = self.diff(before, self._values)
        self._notify(changes)
        return changes

    def update(self, values: Mapping[str, Any], *, source: str = "sim") -> list[Change]:
        if source not in SOURCES:
            raise SignalError(f"unknown source {source!r}")
        coerced = {p: coerce(self.signal(p), v) for p, v in values.items()}
        with self._lock:
            before = dict(self._values)
            for path, value in coerced.items():
                if source == "sim" and self._sources[path] in REAL_SOURCES:
                    continue
                self._values[path] = value
                self._sources[path] = source
            changes = self.diff(before, self._values)
        self._notify(changes)
        return changes

    def release(self, paths: Iterable[str]) -> None:
        """Hand real-sourced paths back to the simulation; their values stay as they are."""
        with self._lock:
            for path in paths:
                self.signal(path)
                if self._sources[path] in REAL_SOURCES:
                    self._sources[path] = "sim"

    def apply(self, overrides: Mapping[str, Any]) -> list[Change]:
        """Episode init: set current (and target, for actuators) regardless of kind."""
        changes = self.update(overrides, source="sim")
        with self._lock:
            for path in overrides:
                if self._signals[path].kind == "actuator":
                    self._targets[path] = self._values[path]
        return changes

    def snapshot(self, *, with_source: bool = False) -> dict[str, Any]:
        with self._lock:
            if with_source:
                return {p: (v, self._sources[p]) for p, v in self._values.items()}
            return dict(self._values)

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        self._listeners.append(listener)

        def _unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return _unsubscribe

    def _notify(self, changes: list[Change]) -> None:
        if not changes:
            return
        for listener in list(self._listeners):
            try:
                listener(changes)
            except Exception:  # noqa: BLE001 -- a UI listener never breaks a vehicle write
                logger.exception("cabingym:listener_failed")

    @staticmethod
    def diff(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[Change]:
        paths = sorted(set(before) | set(after))
        return [Change(p, before.get(p), after.get(p)) for p in paths if before.get(p) != after.get(p)]
