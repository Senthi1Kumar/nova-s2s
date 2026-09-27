# nova_client.py — reference device client

A laptop pretending to be an embedded device: it only moves audio over
`/v1/realtime`. Needs `websockets` (and `sounddevice` for mic/speaker modes);
both are in nova-hailo's `.venv`.

```bash
# on the Pi, once: make the cert (prints its sha256)
bash scripts/make_lan_cert.sh
# on the laptop
scp RPi5-AI-HAT+2:.config/nova-hailo/tls/cert.pem /tmp/nova-cert.pem
export T=<NOVA_HAILO_API_TOKEN>
URL=wss://192.168.1.36:8767/v1/realtime

python nova_client.py --record q_device.wav --seconds 4          # "what time is it?"
python nova_client.py --url $URL --token $T --ca /tmp/nova-cert.pem \
    --wav q_device.wav --out reply.wav --wake device
python nova_client.py --url $URL --token $T --ca /tmp/nova-cert.pem --wake server   # live
python nova_client.py --url $URL --token $T --ca /tmp/nova-cert.pem --wake device   # Enter = wake
```

Exit codes: 0 reply received · 2 no `response.done` before timeout · 3 done but no audio.
