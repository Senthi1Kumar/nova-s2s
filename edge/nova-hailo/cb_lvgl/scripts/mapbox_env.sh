# Sourced by the launchers. cb_load_mapbox_key exports MAPBOX_API_KEY from nova-hailo's .env when the shell does
# not already carry it. It reads only that one variable and never prints it. NOVA_ENV_FILE overrides the path.
cb_load_mapbox_key() {
  [ -n "${MAPBOX_API_KEY:-}" ] && return 0
  local f="${NOVA_ENV_FILE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/.env}"
  [ -f "$f" ] || return 0
  local v
  v=$(sed -n 's/^[[:space:]]*\(export[[:space:]]\+\)\?MAPBOX_API_KEY=//p' "$f" | tail -n 1)
  v=${v%$'\r'}
  v=${v%\"}; v=${v#\"}; v=${v%\'}; v=${v#\'}
  [ -n "$v" ] && export MAPBOX_API_KEY="$v"
  return 0
}
