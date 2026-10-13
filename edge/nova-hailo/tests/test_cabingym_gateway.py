# tests/test_cabingym_gateway.py
from nova_hailo.cabingym import catalog as c
from nova_hailo.edge_harness.policy import CapabilityProfile
from nova_hailo.edge_harness.tool_broker import ToolBroker
from nova_hailo.edge_harness.types import Intent, RoutedIntent
from nova_hailo.tools.oem_tools import OemToolGateway


def gw(**kw):
    return OemToolGateway(enabled=["web_search", "vehicle"], router="regex", **kw)


def test_vehicle_turn_executes_on_the_gateway_cabin():
    g = gw()
    out = g.route_and_execute("set the temperature to 23 degrees")
    assert out["name"] == "vehicle" and out["ok"] and out["speak"] == "Driver side set to 23 degrees."
    assert g.cabin.store.get(c.hvac_temp("Driver")) == 23.0
    assert g.route("open the sunroof") == "vehicle"


def test_web_routing_unchanged():
    assert gw().route("who won the match yesterday") == "web_search"


def test_confirmation_round_trip():
    g = gw()
    g.cabin.store.apply({c.SPEED: 100.0})
    assert g.route_and_execute("open the sunroof")["status"] == "confirm"
    out = g.route_and_execute("yes")
    assert out["status"] == "success" and g.cabin.store.get(c.SUNROOF) == 100


def test_yes_without_pending_is_not_a_vehicle_turn():
    out = gw().route_and_execute("yes")
    assert out is None or out.get("name") != "vehicle"


def test_other_turn_drops_pending():
    g = gw()
    g.cabin.store.apply({c.SPEED: 100.0})
    g.route_and_execute("open the sunroof")
    g.route("who won the match")
    g.route_and_execute("turn the volume up")
    g.route_and_execute("yes")
    assert g.cabin.store.get(c.SUNROOF) == 0


def test_gateway_without_vehicle_is_untouched():
    g = OemToolGateway(enabled=["web_search"], router="regex")
    assert g.cabin is None and g.vehicle is None
    assert g.route("open the sunroof") != "vehicle"


def test_jev_gateway_routes_vehicle_with_fake_client():
    class FakeJev:
        def __init__(self):
            self.answers = [
                {"web_search": {"noul": 0.1}, "dev.windows": {"noul": 0.9}},
                {"windows.set_window": {"noul": 0.9}, "windows.set_window.zone": {"choice": "driver"}},
            ]

        def system_one(self, state, questions, timeout=None):
            return {"answers": self.answers.pop(0)}

    g = OemToolGateway(enabled=["web_search", "vehicle"], router="jev", jev_client=FakeJev(),
                       jev_warm=False)
    out = g.route_and_execute("open my window")
    assert out["name"] == "vehicle"
    assert out["result"]["trace"]["router"] == "jev"
    assert g.cabin.store.get(c.window("Row1.DriverSide")) == 100


def test_broker_without_executor_fails_closed():
    broker = ToolBroker(CapabilityProfile.from_list(["vehicle"]))
    out = broker.execute(RoutedIntent(Intent.VEHICLE, "open the sunroof", {}))
    assert out["ok"] is False and out["reason"] == "vehicle_not_configured"


def _stub_hailo_sdk_if_missing():
    # pipeline.py imports hailo_platform at load; the laptop has no Hailo SDK.
    import sys
    import types
    from unittest.mock import MagicMock

    try:
        import hailo_platform  # noqa: F401
    except ImportError:
        hp = types.ModuleType("hailo_platform")
        hp.VDevice = MagicMock()
        genai = types.ModuleType("hailo_platform.genai")
        genai.LLM = MagicMock()
        genai.Speech2Text = MagicMock()
        genai.Speech2TextTask = MagicMock()
        hp.genai = genai
        sys.modules.setdefault("hailo_platform", hp)
        sys.modules.setdefault("hailo_platform.genai", genai)


def test_pipeline_emits_agent_trace():
    _stub_hailo_sdk_if_missing()
    from nova_hailo.pipeline import NovaPipeline

    p = NovaPipeline.__new__(NovaPipeline)
    seen = []
    p.on_agent_trace = seen.append
    p._emit_agent_trace({"name": "vehicle", "result": {"trace": {"router": "regex"}}})
    assert seen == [{"router": "regex"}]
    p.on_agent_trace = None
    p._emit_agent_trace({"name": "vehicle", "result": {"trace": {}}})
