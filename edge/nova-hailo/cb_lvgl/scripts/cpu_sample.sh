#!/usr/bin/env bash
# CPU use of one process as % of ONE core, averaged over N seconds, from /proc (no sysstat needed).
#   scripts/cpu_sample.sh <pid> [seconds=60]
set -euo pipefail
PID=${1:?usage: cpu_sample.sh <pid> [seconds]}
SECS=${2:-60}
ticks() { awk '{ print $14 + $15 }' "/proc/$PID/stat"; }
T0=$(ticks)
sleep "$SECS"
T1=$(ticks)
HZ=$(getconf CLK_TCK)
awk -v d=$((T1 - T0)) -v hz="$HZ" -v s="$SECS" 'BEGIN { printf "cpu_pct_one_core=%.1f\n", d / hz / s * 100 }'
