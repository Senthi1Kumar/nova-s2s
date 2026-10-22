"""Real GPS through gpsd, which owns the receiver and shares it with cgps, xgps and Nova.

Callers: cabingym.adapters.gps_nmea.build_gps_feeder (when gpsd answers), tests.

gpsd streams JSON reports on TCP 2947 after ``?WATCH={"enable":true,"json":true}``:
TPV carries the fix (mode 2 or 3, lat, lon, speed in m/s, track), SKY the satellites.
Store writes, fix loss and place lookups are the NMEA feeder's.
"""
from __future__ import annotations

import json
import logging
import socket
from collections.abc import Callable
from typing import Any

from nova_hailo.cabingym import catalog as c
from nova_hailo.cabingym.adapters.gps_nmea import GPSD_HOST, GPSD_PORT, GpsFeeder

logger = logging.getLogger(__name__)

_WATCH = b'?WATCH={"enable":true,"json":true};\n'


def _connect(host: str, port: int) -> Any:
    return socket.create_connection((host, port), timeout=5.0)


class GpsdFeeder(GpsFeeder):
    def __init__(self, cabin: Any, *, host: str = GPSD_HOST, port: int = GPSD_PORT,
                 connect: Callable[[str, int], Any] = _connect, **kw: Any) -> None:
        super().__init__(cabin, device=f"gpsd {host}:{port}", **kw)
        self.host, self.port = host, int(port)
        self._connect = connect

    def feed_json(self, line: str) -> None:
        try:
            msg = json.loads(line)
        except (TypeError, ValueError):
            return
        if not isinstance(msg, dict):
            return
        if msg.get("class") == "SKY":
            sats = msg.get("uSat")
            if sats is None and isinstance(msg.get("satellites"), list):
                sats = sum(1 for s in msg["satellites"] if isinstance(s, dict) and s.get("used"))
            if sats is not None:
                self.store.update({c.GPS_SATS: int(sats)}, source="gps")
        elif msg.get("class") == "TPV":
            lat, lon = msg.get("lat"), msg.get("lon")
            fixed = int(msg.get("mode") or 0) >= 2 and lat is not None and lon is not None
            speed = msg.get("speed")
            self._report(fixed, lat, lon, None if speed is None else float(speed) * 3.6,
                         msg.get("track"))

    def run(self) -> None:
        """Read gpsd until stopped; reconnect after errors without flooding the log."""
        warned = False
        while not self._stop.is_set():
            sock = None
            try:
                sock = self._connect(self.host, self.port)
                sock.sendall(_WATCH)
                lines = sock.makefile("r", encoding="utf-8", errors="replace")
                logger.info("gps: reading gpsd at %s:%d", self.host, self.port)
                warned = False
                while not self._stop.is_set():
                    line = lines.readline()
                    if not line:
                        break
                    try:
                        self.feed_json(line)
                    except Exception:  # noqa: BLE001 — one bad report never stops the feed
                        logger.debug("gps: report skipped", exc_info=True)
                    self.check_stale()
            except Exception as exc:  # noqa: BLE001 — no gpsd means the sim drive, not a crash
                if not warned:
                    logger.warning("gps: gpsd at %s:%d unavailable (%s); retrying every %.0f s",
                                   self.host, self.port, exc, self.retry_s)
                    warned = True
            finally:
                if sock is not None:
                    sock.close()
            self.check_stale()
            self._stop.wait(self.retry_s)
