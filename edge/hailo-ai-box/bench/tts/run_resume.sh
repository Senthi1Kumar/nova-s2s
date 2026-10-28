#!/usr/bin/env bash
# Resume after Pi power loss; then re-measure key configs from a cool start into results_cool/.
cd ~/tts-bench
PY=.venv/bin/python
PIPER=/home/cariad/repos/test_nova_repo/nova-s2s/edge/nova-hailo/models/piper
cool() { for i in $(seq 1 18); do t=$(vcgencmd measure_temp | sed "s/[^0-9.]//g" | cut -d. -f1); [ "$t" -le ${COOLT:-72} ] && return; sleep 10; done; }
run() { cool; echo "== $*"; $PY bench_tts.py "$@" 2>&1 | tail -3; }
M18=english_18m_2026-10
run --engine phonon --voice Harper --lang en --threads 4
for t in 1 2 3; do run --engine phonon --voice default --lang en --threads $t; done
run --engine phonon --voice default --lang en --threads 4 --quant q8_0 --tag _q8
for t in 4 2 1; do run --engine piper --model $PIPER/en_US-amy-low.onnx --threads $t; done
for m in en_US-lessac-medium en_US-hfc_female-medium; do run --engine piper --model $PIPER/$m.onnx --threads 4; done
echo PART2DONE
export COOLT=66
O="--out results_cool"
run $O --engine piper --model $PIPER/en_US-amy-low.onnx --threads 4
run $O --engine phonon --voice default --lang en --threads 4
run $O --engine phonon --voice default --lang en --threads 2
run $O --engine pocket --model $M18 --voice alba --threads 4
run $O --engine piper --model $PIPER/en_US-lessac-medium.onnx --threads 4
run $O --engine pocket --model $M18 --voice alba --threads 2
echo ALLDONE
