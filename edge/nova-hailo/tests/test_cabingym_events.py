# tests/test_cabingym_events.py
import json
from pathlib import Path

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.events import vehicle_delta_event, vehicle_snapshot_event
from nova_hailo.cabingym.runtime import CabinEnv
from nova_hailo.cabingym.vss import Change

ROOT = Path(__file__).resolve().parents[1]


def test_snapshot_event_is_json_and_labels_sources():
    cabin = CabinEnv()
    ev = vehicle_snapshot_event(cabin)
    assert ev["type"] == "nova.vehicle" and ev["seq"] == 0
    assert ev["profile"] == "ev_demo" and ev["adapter"] == "sim"
    assert ev["snapshot"][c.SPEED] == {"value": 0.0, "source": "sim"}
    json.dumps(ev)


def test_delta_event():
    ev = vehicle_delta_event([Change(c.SUNROOF, 0, 100)], 7)
    assert ev == {"type": "nova.vehicle", "seq": 7, "changed": {c.SUNROOF: 100}}


def test_protocol_doc_describes_vehicle_events():
    text = (ROOT / "docs/api/realtime.md").read_text()
    assert "`nova.vehicle`" in text and "`nova.agent_trace`" in text
