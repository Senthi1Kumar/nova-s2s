#!/usr/bin/env bash
# LAN realtime security, set up by the launcher itself: a TLS cert for this
# Pi's current IP and an API token under ~/.config/nova-hailo, made on first
# use and exported for nova_hailo.web.serve. Values already set (environment
# or .env) win. NOVA_HAILO_TLS=off keeps plain ws/http on the LAN.
# Callers: scripts/run_web.sh (sourced), tests/test_lan_auth.py.

# A phone browser shows this fingerprint before trusting the self-signed cert.
_nova_cert_sha256() {
  echo "[tls] sha256 $(openssl x509 -in "$1" -noout -fingerprint -sha256 | cut -d= -f2)"
}

nova_lan_auth() {
  if [[ "${NOVA_HAILO_TLS:-on}" == "off" ]]; then
    echo "[tls] off (NOVA_HAILO_TLS=off): plain ws/http on the LAN, no token"
    return 0
  fi
  local here cfg tls ip
  here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  cfg="$HOME/.config/nova-hailo"
  tls="${NOVA_HAILO_TLS_DIR:-$cfg/tls}"
  if [[ -z "${NOVA_HAILO_TLS_CERT:-}" ]]; then
    ip="${NOVA_HAILO_CERT_IP:-$(hostname -I 2>/dev/null | awk '{print $1}')}"
    if [[ ! -s "$tls/cert.pem" || ! -s "$tls/key.pem" ]]; then
      bash "$here/make_lan_cert.sh" >/dev/null || return 1
      echo "[tls] made a certificate for ${ip}: $tls/cert.pem"
      _nova_cert_sha256 "$tls/cert.pem"
    elif [[ -n "$ip" ]] && ! openssl x509 -in "$tls/cert.pem" -noout -ext subjectAltName 2>/dev/null \
        | grep -qE "IP Address:${ip//./\\.}(,|\$)"; then
      # Devices connect by IP, so the cert must name the current one.
      bash "$here/make_lan_cert.sh" >/dev/null || return 1
      echo "[tls] this Pi's IP is now ${ip}: made a new certificate. Copy the new $tls/cert.pem to every device."
      _nova_cert_sha256 "$tls/cert.pem"
    fi
    export NOVA_HAILO_TLS_CERT="$tls/cert.pem" NOVA_HAILO_TLS_KEY="$tls/key.pem"
  fi
  if [[ -z "${NOVA_HAILO_API_TOKEN:-}" ]]; then
    if [[ ! -s "$cfg/api_token" ]]; then
      (umask 077 && mkdir -p "$cfg" && openssl rand -hex 24 > "$cfg/api_token") || return 1
      echo "[tls] made an API token: $cfg/api_token"
    fi
    chmod 600 "$cfg/api_token"
    NOVA_HAILO_API_TOKEN="$(<"$cfg/api_token")"
    export NOVA_HAILO_API_TOKEN
  fi
}

nova_lan_auth || {
  echo "[tls] could not set up the certificate or token (NOVA_HAILO_TLS=off runs without them)" >&2
  return 1
}
