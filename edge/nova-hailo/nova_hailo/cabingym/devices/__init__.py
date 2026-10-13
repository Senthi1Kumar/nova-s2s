# nova_hailo/cabingym/devices/__init__.py
"""CabinGym devices: one module per vehicle function group.

DEVICE_ORDER is also the regex router's matching order: more specific
devices (phone, navigation, media, seats) come before hvac, whose
temperature pattern is the broadest.
"""
from __future__ import annotations

from nova_hailo.cabingym.devices import (
    body, cabin, doors, drive, hvac, lights, media, navigation, phone, seats, system, windows,
)
from nova_hailo.cabingym.devices.base import Device

_MODULES = (phone, navigation, media, seats, windows, body, doors, lights, cabin, drive, system, hvac)
DEVICE_ORDER: tuple[str, ...] = tuple(m.DEVICE.name for m in _MODULES)


def build_devices() -> dict[str, Device]:
    return {m.DEVICE.name: m.DEVICE for m in _MODULES}
