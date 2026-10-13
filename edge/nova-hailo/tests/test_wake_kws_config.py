"""WakeWordDetector.from_config: one reading of the wake.* keys for every door."""
from nova_hailo.wake_kws import WakeWordDetector


class Cfg:
    def __init__(self, raw):
        self.raw = raw

    def get(self, *keys, default=None):
        cur = self.raw
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur


def test_reads_the_wake_keys():
    d = WakeWordDetector.from_config(Cfg({"wake": {
        "kws_enabled": False, "kws_threshold": 0.2, "kws_consecutive_hits": 2,
        "kws_cooldown_s": 3.0, "kws_barge_threshold": 0.05, "kws_model": "m.onnx",
    }}))
    assert (d.threshold, d.consecutive_hits, d.cooldown_s, d.barge_threshold) == (0.2, 2, 3.0, 0.05)
    assert d.model_path == "m.onnx" and d.enabled is False


def test_defaults_match_the_lan_door():
    d = WakeWordDetector.from_config(Cfg({}))
    assert (d.enabled, d.threshold, d.consecutive_hits, d.cooldown_s, d.barge_threshold) == (
        False, 0.15, 1, 1.5, 0.10)
