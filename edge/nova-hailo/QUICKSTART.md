# Nova CabinEnv on a Raspberry Pi: quickstart

Nova is a voice assistant for the car cabin. This runs it on a Raspberry Pi with a full-screen cockpit: a live
Mapbox map with turn-by-turn directions, climate, windows, sunroof, lights and media, all on a simulated car you
control by voice.

**You need**

- Raspberry Pi 5 (8 GB) with the **AI HAT+ 2** (Hailo-10H)
- A **reSpeaker XVF3800** for the mic and speaker (USB; no driver to install). Plug it in before setup. The older **WM8960 Audio HAT** still works; `setup_pi.sh` prints its one-time driver install if that card is what you have. Use one board, not both.
- Raspberry Pi OS **Trixie**, 64-bit, with the desktop, and a display on HDMI

## 1. Get the code

**A. New Pi:**

```bash
git clone https://github.com/Senthi1Kumar/nova-s2s.git
cd nova-s2s/edge/nova-hailo
```

**B. You already have `nova-s2s` from an earlier demo:** update it in place. Your models, `.venv` and
`.env` stay, so setup is much quicker.

```bash
cd ~/nova-s2s                 # where you cloned it before
git status                    # if it lists modified files, run: git stash
git fetch origin
git switch main
git pull
cd edge/nova-hailo
```

## 2. Add the keys

```bash
[ -f .env ] || cp .env.example .env
nano .env
```

Make sure `GROQ_API_KEY` and `MAPBOX_API_KEY` are filled in, plus `EXA_API_KEY` (web search) and `OPENROUTER_API_KEY` (backup model) if you have them.

## 3. Set up (once: about 45 minutes on a new Pi over Wi-Fi, 10–15 minutes when updating)

```bash
scripts/setup_pi.sh
```

It checks the Pi, installs what is missing, downloads the models and the map engine, and builds the
cockpit. If it stops with a message or asks you to reboot, do that and run it again.

## 4. Start

```bash
scripts/run_cabinenv.sh
```

The cockpit opens full screen after about a minute. Then just talk: the mic is open, so everything you say
is a request. `Ctrl+C` in the terminal stops everything. To say **"Hey Nova"** first instead:
`scripts/run_cabinenv.sh --wake-word`.

## Things to try

- "Take me to Mountain View." Nova plans the route, and the car drives it on the map with spoken
  turn-by-turn directions.
- "Take me to Barbara Drive in Los Gatos via Los Altos, Cupertino and Campbell."
- "Find a coffee place on the way", then "add the second one as a stop".
- "Find a charger within 10 minutes."
- "Set the temperature to 21", "open the sunroof", "turn the ambient light purple", "switch to sport mode".
- "What time is it?" (the clock follows the car's location).
- With a USB GPS plugged in: "Where am I?" and "How fast am I going?" answer from the real fix, and a
  route follows the real car. `setup_pi.sh` sets up gpsd for it; Nova finds it on its own.

## If something is off

| What you see | What to do |
| --- | --- |
| `setup_pi.sh` stops with a message | Do what it says, then run it again |
| The map is a grey placeholder | `MAPBOX_API_KEY` is missing in `.env` |
| No sound, or Nova does not react | In the desktop sound settings, input and output are the XVF3800 (it appears as a USB card). The launcher sets its volume. Unplug and replug, then start again. |
| You talk while Nova is speaking and nothing happens | With the XVF3800 the mic stays open: the board cancels its own echo. A short "mm-hm" is only acted on when you start with `NOVA_HAILO_XVF_TURNS=on`; otherwise it is logged. |
| Nova says there's no GPS fix | The receiver needs open sky: a windowsill at least. `cgps` shows how many satellites it sees. Without a fix the simulated car drives, and Nova says so. |
| Nova says it can't reach a service | That key is missing in `.env` (web search needs `EXA_API_KEY`) |
| Anything else | Send Senthil `/tmp/nova_server.log` and `/tmp/cb_lvgl.log` |

**Update later:** `git pull`, then `scripts/setup_pi.sh` again.
