# tests/test_cabingym_vss.py
import pytest

from nova_hailo.cabingym.vss import Change, Signal, SignalError, VssStore


def _store():
    return VssStore([
        Signal("A.Temp", "actuator", "float", unit="celsius", min=16, max=28, default=21.0),
        Signal("A.Fan", "actuator", "int", min=0, max=6, default=2),
        Signal("A.On", "actuator", "bool", default=False),
        Signal("A.Mode", "actuator", "enum", allowed=("off", "auto"), default="auto"),
        Signal("A.List", "actuator", "list", default=()),
        Signal("S.Speed", "sensor", "float", min=0, max=300, default=0.0),
    ])


def test_defaults_and_get():
    s = _store()
    assert s.get("A.Temp") == 21.0
    assert s.get("A.List") == ()
    assert s.source("A.Temp") == "sim"


def test_duplicate_path_rejected():
    with pytest.raises(SignalError):
        VssStore([Signal("X", "sensor", "int"), Signal("X", "sensor", "int")])


@pytest.mark.parametrize("path,value", [
    ("A.Temp", 30.0), ("A.Temp", "21"), ("A.Temp", True), ("A.Fan", 2.5),
    ("A.On", 1), ("A.Mode", "max"), ("Nope", 1), ("S.Speed", 10.0),
])
def test_set_targets_rejects_bad_values(path, value):
    with pytest.raises(SignalError):
        _store().set_targets({path: value})


def test_set_targets_all_or_nothing():
    s = _store()
    with pytest.raises(SignalError):
        s.set_targets({"A.Temp": 22.0, "A.Fan": 99})
    assert s.get("A.Temp") == 21.0


def test_set_targets_returns_changes_and_records_target():
    s = _store()
    assert s.set_targets({"A.Temp": 22.5, "A.Fan": 2}) == [Change("A.Temp", 21.0, 22.5)]
    assert s.target("A.Temp") == 22.5


def test_int_accepts_integral_float():
    s = _store()
    s.set_targets({"A.Fan": 3.0})
    assert s.get("A.Fan") == 3 and isinstance(s.get("A.Fan"), int)


def test_target_sink_controls_current():
    s = _store()
    s.target_sink = lambda path, value: None
    s.set_targets({"A.Temp": 25.0})
    assert s.get("A.Temp") == 21.0
    assert s.target("A.Temp") == 25.0


def test_real_source_wins_over_sim():
    s = _store()
    s.update({"S.Speed": 42.0}, source="obd")
    s.update({"S.Speed": 0.0}, source="sim")
    assert s.get("S.Speed") == 42.0 and s.source("S.Speed") == "obd"
    s.update({"S.Speed": 50.0}, source="obd")
    assert s.get("S.Speed") == 50.0


def test_unknown_source_rejected():
    with pytest.raises(SignalError):
        _store().update({"S.Speed": 1.0}, source="can")


def test_apply_sets_sensor_and_target():
    s = _store()
    s.apply({"S.Speed": 80.0, "A.Temp": 19.0})
    assert s.get("S.Speed") == 80.0
    assert s.target("A.Temp") == 19.0


def test_snapshot_with_source():
    s = _store()
    s.update({"S.Speed": 5.0}, source="gps")
    assert s.snapshot(with_source=True)["S.Speed"] == (5.0, "gps")
    assert s.snapshot()["S.Speed"] == 5.0


def test_subscribe_and_unsubscribe():
    s = _store()
    seen = []
    unsub = s.subscribe(seen.append)
    s.set_targets({"A.On": True})
    unsub()
    s.set_targets({"A.On": False})
    assert seen == [[Change("A.On", False, True)]]


def test_no_notification_without_change():
    s = _store()
    seen = []
    s.subscribe(seen.append)
    s.set_targets({"A.Temp": 21.0})
    assert seen == []


def test_listener_exception_does_not_break_write():
    s = _store()

    def boom(_changes):
        raise RuntimeError("socket closed")

    s.subscribe(boom)
    assert s.set_targets({"A.On": True})
    assert s.get("A.On") is True


def test_list_values_stored_as_tuples():
    s = _store()
    s.set_targets({"A.List": [{"id": "m1"}]})
    assert s.get("A.List") == ({"id": "m1"},)


def test_diff_is_sorted_and_minimal():
    assert VssStore.diff({"b": 1, "a": 1}, {"b": 2, "a": 1, "c": 3}) == [
        Change("b", 1, 2), Change("c", None, 3)]
