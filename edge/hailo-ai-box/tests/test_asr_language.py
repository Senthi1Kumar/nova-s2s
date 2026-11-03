from types import SimpleNamespace

import numpy as np
import pytest

from aibox.backends.asr import HailoWhisper
from aibox.config import Settings, load_settings


class FakeS2T:
    def __init__(self):
        self.kwargs = None

    def generate_all_text(self, pcm, **kwargs):
        self.kwargs = kwargs
        return "hello"


def test_whisper_defaults_to_english():
    s2t = FakeS2T()
    pool = SimpleNamespace(settings=Settings(), vdevice=None)
    whisper = HailoWhisper(pool)
    whisper._s2t = s2t
    assert whisper._run(np.zeros(160, dtype=np.float32), None) == "hello"
    assert s2t.kwargs == {"language": "en"}


def test_asr_language_can_be_overridden_per_call_and_environment():
    s2t = FakeS2T()
    pool = SimpleNamespace(settings=load_settings({"AIBOX_ASR_LANGUAGE": "en"}), vdevice=None)
    whisper = HailoWhisper(pool)
    whisper._s2t = s2t
    whisper._run(np.zeros(160, dtype=np.float32), "fr")
    assert s2t.kwargs == {"language": "fr"}


def test_whisper_defaults_to_small():
    whisper = HailoWhisper(SimpleNamespace(settings=Settings(), vdevice=None))
    assert whisper.model_id == "whisper-small"
    assert whisper.hef.endswith("/Whisper-Small.hef")


def test_whisper_model_can_be_selected_from_environment():
    settings = load_settings({"AIBOX_ASR_MODEL": "whisper-base"})
    whisper = HailoWhisper(SimpleNamespace(settings=settings, vdevice=None))
    assert whisper.model_id == "whisper-base"
    assert whisper.hef.endswith("/Whisper-Base.hef")


def test_whisper_model_rejects_unknown_names():
    with pytest.raises(ValueError, match="AIBOX_ASR_MODEL"):
        load_settings({"AIBOX_ASR_MODEL": "whisper-large"})
