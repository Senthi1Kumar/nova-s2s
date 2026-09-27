# Nova realtime — ESP32-S3 reference firmware

Audio I/O only: mic → Pi, Pi → speaker. Contract: `docs/api/realtime.md`.
Default pins: Waveshare ESP32-S3-Touch-LCD-1.85C **V1**. Other boards: edit `main/board.h`.

1. On the Pi: `bash scripts/make_lan_cert.sh`, then copy `~/.config/nova-hailo/tls/cert.pem` over `main/nova_cert.pem`.
2. `idf.py set-target esp32s3 && idf.py menuconfig`
   - Example Connection Configuration → Wi-Fi SSID / password
   - Nova realtime → URL, token, wake mode, echo cancellation
3. `idf.py build flash monitor`

Wake: the BOOT button calls `nova_local_wake()`. For a real wake word ("Hi ESP"
via ESP-SR WakeNet), call `nova_local_wake()` from its detection callback.
Choosing "server" wake uses the Pi's "Hey Nova" and streams the mic continuously.

Status: compiles cleanly with ESP-IDF v5.4 (no warnings; app binary ~0.86 MB).
Not yet run on hardware.

Build without installing ESP-IDF (Docker, from this folder):

```bash
sudo docker run --rm -v "$PWD":/project -w /project espressif/idf:v5.4 \
  bash -c "idf.py set-target esp32s3 && idf.py build; chown -R $(id -u):$(id -g) /project"
```

The default partition table gives the app 1 MB, with ~14% free. Adding
ESP-SR WakeNet or an LVGL UI will need more: set
`CONFIG_PARTITION_TABLE_SINGLE_APP_LARGE=y` or use a custom `partitions.csv`
(the board has 16 MB of flash).
