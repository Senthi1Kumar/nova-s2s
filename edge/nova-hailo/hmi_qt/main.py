#!/usr/bin/env python3
"""Nova HMI — Qt 6 / QML client for nova-hailo /v1/realtime.

Default is live: mic + speakers + WebSocket to the local backend.

    # Pi (demo already running):
    cd ~/nsk/nova-hailo/hmi_qt && ./run.sh

    # Laptop (SSH tunnel, same as the web UI):
    ssh -L 8766:localhost:8766 cariad@192.168.1.35
    cd edge/nova-hailo/hmi_qt && ./run.sh
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl
from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow

from nova_hmi.audio import AudioDuplex
from nova_hmi.controller import NovaController

ROOT = Path(__file__).resolve().parent
QML_ROOT = ROOT if (ROOT / "NovaUI" / "Main.qml").is_file() else ROOT / "NovaUI"


def parse_args(argv):
    p = argparse.ArgumentParser(description="Nova HMI (Qt / QML)")
    p.add_argument(
        "--ws",
        default=os.environ.get("NOVA_HAILO_WS_URL")
        or os.environ.get("NOVA_WS")
        or "ws://127.0.0.1:8766/v1/realtime",
        help="Backend websocket (default: ws://127.0.0.1:8766/v1/realtime)",
    )
    p.add_argument("--demo", action="store_true", help="scripted UI only, no backend")
    p.add_argument("--mic", action="store_true", help="force microphone capture")
    p.add_argument("--no-mic", action="store_true", help="no microphone uplink")
    p.add_argument("--kiosk", action="store_true", help="borderless full screen (Esc quits)")
    p.add_argument("--software", action="store_true", help="software rasteriser")
    return p.parse_known_args(argv)[0]


def main(argv=None) -> int:
    argv = sys.argv if argv is None else argv
    args = parse_args(argv[1:])
    if args.software:
        QQuickWindow.setSceneGraphBackend("software")

    app = QGuiApplication(argv)
    app.setApplicationName("Nova HMI")
    app.setOrganizationName("Elevatics AI")
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    keepalive = QTimer()
    keepalive.start(250)
    keepalive.timeout.connect(lambda: None)

    live = bool(args.ws) and not args.demo
    capture = live and not args.no_mic
    if args.mic:
        capture = True
    print(f"[hmi] live={live} ws={args.ws if live else '(demo)'} capture={capture}", flush=True)
    if live:
        import urllib.request

        http = args.ws.replace("ws://", "http://", 1).replace("wss://", "https://", 1)
        http = http.split("/v1/")[0] + "/config"
        try:
            with urllib.request.urlopen(http, timeout=2) as r:
                print(f"[hmi] backend GET /config → {r.status}", flush=True)
        except Exception as exc:
            print(
                f"[hmi] backend not reachable at {http} ({exc})\n"
                "      Start ./scripts/run_demo_oem.sh on the Pi first.\n"
                "      On a laptop keep `ssh -L 8766:localhost:8766 …` open "
                "and run this script in a local terminal.",
                flush=True,
            )

    audio = AudioDuplex(capture=capture, playback=live)
    bridge = None
    if live:
        try:
            from nova_hmi.bridge import RealtimeBridge
        except ImportError:
            print("QtWebSockets missing — pip install PySide6", file=sys.stderr)
            return 1
        bridge = RealtimeBridge(args.ws)

    controller = NovaController(audio, bridge)
    if bridge is not None:
        bridge.open()

    engine = QQmlApplicationEngine()
    engine.addImportPath(str(ROOT))
    engine.rootContext().setContextProperty("nova", controller)
    qml_main = ROOT / "NovaUI" / "Main.qml"
    engine.load(QUrl.fromLocalFile(str(qml_main)))
    if not engine.rootObjects():
        print(f"QML failed to load: {qml_main}", file=sys.stderr)
        return 1

    rc = app.exec()
    audio.stop()
    if bridge is not None:
        bridge.close()
    return rc


if __name__ == "__main__":
    sys.exit(main())
