# Provisioning the Pi for a new device

`<pi>` is the SSH host (or `user@ip`). `<repo>` is the nova-hailo checkout the
server runs from (step 1). A Pi can hold stale copies with the same name.

The Pi's launcher makes its own TLS certificate (for the Pi's current IP) and
API token in `~/.config/nova-hailo/` on its first run, and loads them every
time. Provisioning is therefore: find the live checkout, make sure the server
listens with TLS, and give the device the certificate and the token.

## Can the agent reach the Pi?

```bash
ssh -o BatchMode=yes -o ConnectTimeout=5 <pi> true && echo ssh-ok
```

`ssh-ok` → run the checks below yourself. Otherwise print the commands for the user
and continue once they report the results. In commands the user types in their own
shell, read files with `$(<file)` rather than `cat`, which may be aliased.

## 1. Find the live checkout

```bash
ssh <pi> 'readlink /proc/$(pgrep -f "[n]ova_hailo.web.serve" | head -1)/cwd 2>/dev/null'
```

A path → that is `<repo>`. Nothing (the server is down) → ask the user where the
checkout is; do not pick one with `find`. The live checkout contains
`scripts/lan_auth.sh`.

## 2. Server listening with TLS

```bash
ssh <pi> 'ss -ltn | grep -q ":8767 " && echo tls-up || echo down'
```

Check the port, not a log: logs outlive the process.

- `tls-up` → ready.
- `down` → ask the user to start it, or ask before starting it yourself:

  ```bash
  ssh <pi> 'cd <repo> && nohup ./scripts/run_demo_oem.sh > /tmp/nova_web.log 2>&1 < /dev/null &'
  ```

  Nothing else to set up: the launcher makes the certificate and the token if
  they are missing. It is ready when port 8767 listens (about 15 s).
- The server runs but 8767 is closed (it was started with `NOVA_HAILO_TLS=off`)
  → **ask the user before restarting**: a restart ends any live session, such as
  the Qt HMI.

The bracket in `[n]ova_hailo` keeps `pgrep` from matching the ssh shell's own
command line.

## 3. Certificate and token for the device

```bash
scp <pi>:.config/nova-hailo/tls/cert.pem <device-project>/
```

ESP-IDF: copy it over `main/nova_cert.pem` (embedded at build time). If the Pi's
address changes, the launcher makes a new certificate on its next start; copy it
again.

Never print the token into logs, chat or committed files, and never turn on
verbose HTTP tracing (libcurl `CURLOPT_VERBOSE`, `curl -v`): it prints the
`Authorization` header. Read the token into an environment variable in the same
shell command that runs the client:

```bash
export NOVA_HAILO_API_TOKEN=$(ssh <pi> command cat .config/nova-hailo/api_token) && <client command>
```

## Token on the device (never committed)

| Platform | Where the token lives |
|---|---|
| ESP-IDF | `idf.py menuconfig` → gitignored `sdkconfig`, or NVS |
| Linux / desktop / Pi | `NOVA_HAILO_API_TOKEN` environment variable |
| Phone | The OS keychain |
| Browser | `?token=` on the page URL (the orb page carries it to the socket) |
