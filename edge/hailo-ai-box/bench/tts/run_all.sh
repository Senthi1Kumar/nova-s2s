#!/usr/bin/env bash
# Runs the whole matrix on the Pi. Cools to <=72C (max ~80s) between runs (no fan), then waits for load<2 inside bench_tts.py.
cd ~/tts-bench
PY=.venv/bin/python
PIPER=/home/cariad/repos/test_nova_repo/nova-s2s/edge/nova-hailo/models/piper
cool() { for i in $(seq 1 8); do t=$(vcgencmd measure_temp | sed "s/[^0-9.]//g" | cut -d. -f1); [ "$t" -le 72 ] && return; sleep 10; done; }
run() { cool; echo "== $*"; $PY bench_tts.py "$@" 2>&1 | tail -3; }
POCKET_VOICES=${POCKET_VOICES-""}
M18=english_18m_2026-10
# pocket voices @4 threads
for v in $POCKET_VOICES; do run --engine pocket --model $M18 --voice $v --threads 4; done
# pocket thread sweep
for t in 1 2 3; do run --engine pocket --model $M18 --voice alba --threads $t; done
# pocket other sizes
run --engine pocket --model english_7m_2026-10 --voice alba --threads 4
run --engine pocket --model english --voice alba --threads 4
# phonon voices @4
for v in default Easton Harper; do run --engine phonon --voice $v --lang en --threads 4; done
for t in 1 2 3; do run --engine phonon --voice default --lang en --threads $t; done
run --engine phonon --voice default --lang en --threads 4 --quant q8_0 --tag _q8
# piper
for t in 4 2 1; do run --engine piper --model $PIPER/en_US-amy-low.onnx --threads $t; done
for m in en_US-lessac-medium en_US-hfc_female-medium; do run --engine piper --model $PIPER/$m.onnx --threads 4; done
echo ALLDONE
