import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


@pytest.mark.skipif(shutil.which("cc") is None, reason="no C compiler")
def test_voice_pcm_harness(tmp_path):
    exe = tmp_path / "test_voice_pcm"
    build = subprocess.run(
        [
            "cc", "-std=c11", "-O1", "-Wall", "-Werror",
            str(ROOT / "studio_lvgl/tests/test_voice_pcm.c"),
            str(ROOT / "studio_lvgl/src/voice_pcm.c"),
            "-lm", "-o", str(exe),
        ],
        capture_output=True,
        text=True,
    )
    assert build.returncode == 0, build.stderr
    run = subprocess.run([str(exe)], capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stdout + run.stderr
