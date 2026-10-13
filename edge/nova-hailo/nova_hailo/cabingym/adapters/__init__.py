# nova_hailo/cabingym/adapters/__init__.py
"""Vehicle adapters: where actuator targets go and where real signals come from."""
from nova_hailo.cabingym.adapters.base import VehicleAdapter
from nova_hailo.cabingym.adapters.sim import InProcessSim

__all__ = ["InProcessSim", "VehicleAdapter"]
