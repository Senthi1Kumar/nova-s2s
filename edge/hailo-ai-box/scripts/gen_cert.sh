#!/usr/bin/env bash
# Self-signed TLS cert for the AI Box (CN = hostname, SAN = LAN IP + localhost). Idempotent.
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="${AIBOX_CERT_DIR:-runtime/certs}"
mkdir -p "$DIR"
if [[ -f "$DIR/cert.pem" && -f "$DIR/key.pem" && "${1:-}" != "--force" ]]; then
  echo "cert exists: $DIR/cert.pem (use --force to regenerate)"
  exit 0
fi
HOST="$(hostname)"
LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
SAN="DNS:${HOST},DNS:localhost,IP:127.0.0.1"
[[ -n "$LAN_IP" ]] && SAN="${SAN},IP:${LAN_IP}"
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -keyout "$DIR/key.pem" -out "$DIR/cert.pem" \
  -subj "/CN=${HOST}" -addext "subjectAltName=${SAN}" 2>/dev/null
chmod 600 "$DIR/key.pem"
echo "generated $DIR/cert.pem (SAN: $SAN)"
