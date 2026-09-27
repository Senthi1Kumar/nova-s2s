#!/usr/bin/env bash
# Self-signed TLS cert for the LAN realtime API (wss://<pi>:8767/v1/realtime).
# Callers: operator on the Pi, tests/test_serve_tls.py.
# Devices trust exactly this cert (ESP: embed cert.pem; python client: --ca).
# The printed SHA-256 is for a human accepting it once in a phone browser.
set -euo pipefail
OUT="${NOVA_HAILO_TLS_DIR:-$HOME/.config/nova-hailo/tls}"
IP="${NOVA_HAILO_CERT_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
if [[ -z "$IP" ]]; then
  echo "Could not detect the LAN IP; set NOVA_HAILO_CERT_IP" >&2
  exit 1
fi
mkdir -p "$OUT"
chmod 700 "$OUT"
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout "$OUT/key.pem" -out "$OUT/cert.pem" -days 825 \
  -subj "/CN=nova.local" \
  -addext "subjectAltName=DNS:nova.local,DNS:localhost,IP:${IP},IP:127.0.0.1" \
  2>/dev/null
chmod 600 "$OUT/key.pem"
FP="$(openssl x509 -in "$OUT/cert.pem" -noout -fingerprint -sha256 | cut -d= -f2)"
echo "cert: $OUT/cert.pem"
echo "key: $OUT/key.pem"
echo "sha256: $FP"
echo
echo "export NOVA_HAILO_TLS_CERT=$OUT/cert.pem NOVA_HAILO_TLS_KEY=$OUT/key.pem"
