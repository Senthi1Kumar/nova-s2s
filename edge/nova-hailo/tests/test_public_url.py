from nova_hailo.web.public_url import public_ws_url


def test_plain_request_gives_ws_on_same_host_and_port():
    assert public_ws_url(scheme="http", host_header="192.168.1.36:8766", default_host="localhost",
                         default_port=8766, query_token=None) == "ws://192.168.1.36:8766/v1/realtime"


def test_https_request_gives_wss():
    assert public_ws_url(scheme="https", host_header="192.168.1.36:8767", default_host="localhost",
                         default_port=8766, query_token=None) == "wss://192.168.1.36:8767/v1/realtime"


def test_caller_token_is_echoed_url_encoded():
    url = public_ws_url(scheme="https", host_header="nova.local:8767", default_host="localhost",
                        default_port=8766, query_token="a b&c")
    assert url == "wss://nova.local:8767/v1/realtime?token=a+b%26c"


def test_missing_host_header_uses_defaults():
    assert public_ws_url(scheme="http", host_header="", default_host="pi", default_port=8766,
                         query_token=None) == "ws://pi:8766/v1/realtime"


def test_host_without_port_keeps_default_port():
    assert public_ws_url(scheme="http", host_header="nova.local", default_host="x", default_port=8766,
                         query_token=None) == "ws://nova.local:8766/v1/realtime"
