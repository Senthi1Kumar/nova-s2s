import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs/api/realtime.md"
COPY = ROOT / "skills/nova-realtime-client/references/protocol.md"


def test_skill_protocol_is_identical_to_public_doc():
    # The skill folder gets copied into other people's agents on its own, so
    # it carries a real file, not a link -- this keeps the two from drifting.
    assert COPY.read_bytes() == DOC.read_bytes()


def test_doc_names_the_contract_values():
    text = DOC.read_text()
    assert "nova-realtime/1" in text
    for rate in ("16000", "22050", "24000", "48000"):
        assert rate in text
    for err in ("invalid_audio_frame", "unsupported_sample_rate", "invalid_session_update",
                "session_limit_reached", "deny_bad_token", "deny_no_token_configured"):
        assert f"`{err}`" in text


UI_STATE = ROOT / "docs/api/ui-state.md"
UI_STATE_COPIES = [
    ROOT / "skills/nova-realtime-client/references/ui-state.md",
    ROOT / "skills/nova-lvgl-ui/references/ui-state.md",
]


def test_ui_state_copies_are_identical_to_public_doc():
    for copy in UI_STATE_COPIES:
        assert copy.read_bytes() == UI_STATE.read_bytes(), copy


def test_ui_state_doc_names_every_field_and_state():
    text = UI_STATE.read_text()
    for field in ("`conn`", "`state`", "`you`", "`nova`", "`level`", "`error`", "`wake`"):
        assert field in text
    for state in ("IDLE", "LISTENING", "THINKING", "SPEAKING", "ARMED", "INTERRUPTING", "TRANSCRIBING"):
        assert state in text


SKILL = ROOT / "skills/nova-realtime-client/SKILL.md"


def test_realtime_skill_covers_intake_provision_verify():
    text = SKILL.read_text()
    for heading in ("## 1. Intake", "## 2. Provision", "## 3. Verify the Pi first",
                    "## 4. Decide", "## 5. Build", "## 6. Verify the device"):
        assert heading in text, heading
    assert "references/provisioning.md" in text
    assert "samples/what-time-is-it.wav" in text
    assert (ROOT / "examples/realtime/python/samples/what-time-is-it.wav").exists()


LVGL_SKILL = ROOT / "skills/nova-lvgl-ui"


def test_lvgl_skill_has_its_references_and_rules():
    skill = (LVGL_SKILL / "SKILL.md").read_text()
    for ref in ("references/lvgl-docs-map.md", "references/nova-face.md", "references/ui-state.md"):
        assert ref in skill and (LVGL_SKILL / ref).exists(), ref
    assert "llms-full.txt" in skill                      # the "never load" rule is stated
    assert "examples/realtime/lvgl" in skill
    doc_map = (LVGL_SKILL / "references/lvgl-docs-map.md").read_text()
    assert "https://lvgl.io/docs/open/llms.mdx/" in doc_map


FIXTURES = [ROOT / "examples/realtime/python/samples/what-time-is-it.wav",
            ROOT / "examples/realtime/lvgl/demo/states.jsonl"]


def test_example_fixtures_are_not_gitignored():
    if shutil.which("git") is None or subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse"], capture_output=True).returncode != 0:
        pytest.skip("not a git checkout")
    for f in FIXTURES:
        r = subprocess.run(["git", "-C", str(ROOT), "check-ignore", "-q", str(f)])
        assert r.returncode == 1, f"{f.relative_to(ROOT)} is gitignored; a commit would drop it"


GETTING_STARTED = ROOT / "docs/api/getting-started.md"


def test_getting_started_leads_with_the_skills_and_the_launcher():
    text = GETTING_STARTED.read_text()
    assert "skills/nova-realtime-client" in text and "skills/nova-lvgl-ui" in text
    assert "./scripts/run_demo_oem.sh" in text
    assert "make_lan_cert" not in text  # the launcher makes the certificate itself
    assert "docs/api/getting-started.md" in (ROOT / "docs/api/realtime.md").read_text()
