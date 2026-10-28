import httpx

from aibox.__main__ import build_services_or_fake, listener_configs
from aibox.app import create_app
from aibox.config import Settings


def _static(tmp_path):
    (tmp_path / "js").mkdir()
    (tmp_path / "index.html").write_text("<html><title>AI BOX</title></html>")
    (tmp_path / "js" / "api.js").write_text("export const apiBase = 1;")
    return tmp_path


async def test_static_index_served(services, tmp_path):
    app = create_app(services, static_dir=_static(tmp_path))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t") as c:
        r = await c.get("/")
        assert r.status_code == 200 and "text/html" in r.headers["content-type"]
        assert "AI BOX" in r.text
        assert (await c.get("/js/api.js")).text.startswith("export")
        assert (await c.get("/health")).json()["status"] == "ok"  # not shadowed
        assert (await c.get("/v1/models")).json()["object"] == "list"


def test_build_listeners():
    https, http = listener_configs(Settings(), env={"AIBOX_CERT_DIR": "/x/certs"})
    assert (https.host, https.port) == ("0.0.0.0", 8443)
    assert https.ssl_certfile == "/x/certs/cert.pem" and https.ssl_keyfile == "/x/certs/key.pem"
    assert (http.host, http.port) == ("127.0.0.1", 8442) and http.ssl_certfile is None
    a, b = listener_configs(Settings(https_port=9443, http_port=9442))
    assert (a.port, b.port) == (9443, 9442)


def test_fake_selected():
    s = build_services_or_fake(Settings(fake=True))
    assert type(s.chat).__name__ == "FakeChat"
