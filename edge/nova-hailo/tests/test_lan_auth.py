"""scripts/lan_auth.sh: the launcher's own TLS cert and API token (secure by default)."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/lan_auth.sh"
pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")

# Same strict mode as scripts/run_web.sh, which sources the helper.
_PROBE = ('set -euo pipefail; source "{script}"; t="${{NOVA_HAILO_API_TOKEN:-}}"; '
          'echo "CERT=${{NOVA_HAILO_TLS_CERT:-}}"; echo "KEY=${{NOVA_HAILO_TLS_KEY:-}}"; '
          'echo "TOKLEN=${{#t}}"')


def _run(home: Path, ip: str = "10.1.2.3", **env) -> subprocess.CompletedProcess:
    e = {"HOME": str(home), "PATH": os.environ["PATH"], "NOVA_HAILO_CERT_IP": ip, **env}
    return subprocess.run(["bash", "-c", _PROBE.format(script=SCRIPT)], env=e,
                          capture_output=True, text=True, timeout=60)


def _vars(r: subprocess.CompletedProcess) -> dict[str, str]:
    return dict(line.split("=", 1) for line in r.stdout.splitlines()
                if line.split("=", 1)[0] in ("CERT", "KEY", "TOKLEN"))


def _san(cert: Path) -> str:
    return subprocess.run(["openssl", "x509", "-in", str(cert), "-noout", "-ext", "subjectAltName"],
                          capture_output=True, text=True).stdout


def test_run_web_sources_the_helper():
    assert 'source "$ROOT/scripts/lan_auth.sh"' in (ROOT / "scripts/run_web.sh").read_text()


def test_first_run_creates_cert_key_and_token(tmp_path):
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    cfg = tmp_path / ".config/nova-hailo"
    v = _vars(r)
    assert v["CERT"] == str(cfg / "tls/cert.pem") and v["KEY"] == str(cfg / "tls/key.pem")
    assert (cfg / "tls/cert.pem").is_file() and (cfg / "tls/key.pem").is_file()
    assert "IP Address:10.1.2.3" in _san(cfg / "tls/cert.pem")
    token = (cfg / "api_token").read_text().strip()
    assert len(token) == 48 and int(token, 16) >= 0
    assert stat.S_IMODE((cfg / "api_token").stat().st_mode) == 0o600
    assert v["TOKLEN"] == "48"
    assert token not in r.stdout + r.stderr  # never printed
    fp = subprocess.run(["openssl", "x509", "-in", str(cfg / "tls/cert.pem"), "-noout",
                         "-fingerprint", "-sha256"], capture_output=True, text=True).stdout
    assert fp.split("=", 1)[1].strip() in r.stdout  # phone browsers compare it


def test_later_runs_reuse_cert_and_token(tmp_path):
    _run(tmp_path)
    cfg = tmp_path / ".config/nova-hailo"
    cert, token = (cfg / "tls/cert.pem").read_bytes(), (cfg / "api_token").read_text()
    r = _run(tmp_path)
    assert r.returncode == 0, r.stderr
    assert (cfg / "tls/cert.pem").read_bytes() == cert
    assert (cfg / "api_token").read_text() == token


def test_ip_change_makes_a_new_cert_and_keeps_the_token(tmp_path):
    _run(tmp_path, ip="10.1.2.3")
    cfg = tmp_path / ".config/nova-hailo"
    cert, token = (cfg / "tls/cert.pem").read_bytes(), (cfg / "api_token").read_text()
    r = _run(tmp_path, ip="10.1.2.4")
    assert r.returncode == 0, r.stderr
    assert (cfg / "tls/cert.pem").read_bytes() != cert
    assert "IP Address:10.1.2.4" in _san(cfg / "tls/cert.pem")
    assert (cfg / "api_token").read_text() == token
    assert "cert.pem" in r.stdout  # tells the operator to re-copy it to devices


def test_tls_off_keeps_plain_mode(tmp_path):
    r = _run(tmp_path, NOVA_HAILO_TLS="off")
    assert r.returncode == 0, r.stderr
    assert _vars(r) == {"CERT": "", "KEY": "", "TOKLEN": "0"}
    assert not (tmp_path / ".config").exists()


def test_values_already_set_win(tmp_path):
    r = _run(tmp_path, NOVA_HAILO_TLS_CERT="/x/c.pem", NOVA_HAILO_TLS_KEY="/x/k.pem",
             NOVA_HAILO_API_TOKEN="abc")
    assert r.returncode == 0, r.stderr
    assert _vars(r) == {"CERT": "/x/c.pem", "KEY": "/x/k.pem", "TOKLEN": "3"}
    assert not (tmp_path / ".config").exists()
