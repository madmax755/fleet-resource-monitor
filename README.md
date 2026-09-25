# Fleet Resource Monitor

Independent Raspberry Pi watcher that SSHes over Tailscale to always-on hosts,
checks disk / RAM / load / temperature, and pages a self-hosted [ntfy](https://ntfy.sh) topic
on threshold crossings and recoveries.

Designed to sit next to Uptime Kuma — not a replacement for it. No Prometheus,
no docker prune, just resource paging with no silent death if the checker dies.

## Features

- Docker Compose deployment (classic `docker-compose` friendly on Pi)
- Inventory-driven hosts (YAML); direct Tailscale SSH by default (optional `ProxyJump`)
- Localhost self-check for the Pi (including extra mounts like `/media/drive1`)
- Disk warn ≥85% / critical ≥92% (configurable; optional extra mounts)
- RAM critical ≥90% using `MemAvailable`
- Load alert if `load1 ≥ 2× nproc`
- Temperature warn ≥70°C / critical ≥80°C when a thermal sensor is readable (hottest `/sys/class/thermal/thermal_zone*/temp`, optionally maxed with `vcgencmd measure_temp` on Pi); skipped silently when no sensor (e.g. Proxmox LXCs)
- **Consecutive confirmation** (default 2 checks ≈ 30m at 15m interval) for disk, RAM, load, and host unreachable — both into alert and back to OK — to damp ephemeral flaps. Temperature defaults to **1** so a single hot reading can page (thermal runaway is too fast for a 30-minute wait)
- ntfy alerts with high priority for critical events (example topic: `fleet-resources`)
- State file: alert on **confirmed state change**, re-alert every **6 hours** while still unhealthy
- Heartbeat file + Docker `HEALTHCHECK` so a wedged loop is visible
- Checker failures themselves attempt an ntfy page
- `--once` / `--dry-run` for safe first runs

## Layout

```
fleet-resource-monitor/
  Dockerfile
  docker-compose.yml
  .env.example
  config/hosts.example.yml
  src/fleet_monitor/     # application
  scripts/healthcheck.sh
  tests/
  README.md
```

## Alert policy (state file)

Persisted in `STATE_FILE` (default `/data/state.json`):

1. **Consecutive confirmation** — a new severity (warn/critical/unreachable **or** recovery to OK) must be observed for N consecutive checks before it is confirmed. Defaults are 2 for disk, RAM, load, and unreachable (`*_CONSECUTIVE_REQUIRED`), and **1** for temperature (`TEMP_CONSECUTIVE_REQUIRED`) so a single hot reading can page. At the default 15-minute interval, N=2 is about 30 minutes before the first page or clear — enough to kill one-shot flaps without hiding real problems; waiting that long for thermal runaway is too slow.
2. **Transition alert** — when a severity is confirmed (including host unreachable).
3. **Recovery alert** — when OK / reachable is confirmed after an unhealthy state.
4. **Cooldown re-alert** — if still confirmed unhealthy after `ALERT_COOLDOWN_SECONDS` (default `21600` = 6h), page again so long-running problems are not forgotten, without spamming every 15-minute loop.

Legacy state files without the new consecutive fields are migrated in place: already-confirmed conditions stay confirmed (no re-alert storm); in-progress load streaks are preserved.

## Quick start (clone)

```bash
git clone https://github.com/madmax755/fleet-resource-monitor.git
cd fleet-resource-monitor
```

## Pi deploy

### 1. Clone onto the Pi

```bash
git clone https://github.com/madmax755/fleet-resource-monitor.git
cd fleet-resource-monitor
```

### 2. Configure

```bash
cp .env.example .env
cp config/hosts.example.yml config/hosts.yml
mkdir -p ssh data

# Install the SSH identity used to reach Tailscale hosts (read-only in compose)
# Key comment on the Pi deploy is typically: pi-fleet-monitor@raspberrypi
cp ~/.ssh/id_ed25519 ssh/id_ed25519
chmod 600 ssh/id_ed25519

# known_hosts must already contain the fleet host keys
cp ~/.ssh/known_hosts ssh/known_hosts
# or: ssh-keyscan -H <tailscale-ip> >> ssh/known_hosts
```

Edit `.env`:

- `NTFY_URL` — e.g. `https://ntfy.your.domain`
- `NTFY_TOPIC` — e.g. `fleet-resources`
- `NTFY_TOKEN` — optional bearer token if the topic is protected

Edit `config/hosts.yml` if IPs/users differ. The recommended inventory uses **direct Tailscale SSH** (no `ProxyJump`). Optional `proxy_jump: server1` (or a `user@host` string) is still supported if a host is only reachable via a jump.

Ensure the identity can SSH (`BatchMode=yes`) to each Tailscale peer.

### 3. Compose notes (Pi self-check)

`docker-compose.yml` bind-mounts `/media/drive1:/media/drive1:ro` so the local Pi self-check can see extra mounts listed under `extra_mounts` in the inventory. Adjust or remove that volume if your Pi layout differs. Compose uses `network_mode: host` so the container shares the Pi’s Tailscale routing and can self-check `localhost`.

### 4. Dry-run once

```bash
docker-compose build
docker-compose run --rm fleet-monitor python -m fleet_monitor --once --dry-run -v
```

You should see per-host metrics and any ntfy payloads logged without publishing.

### 5. Start 24/7

```bash
docker-compose up -d
docker-compose logs -f fleet-monitor
```

Default interval is **900 seconds (15 minutes)**. Classic `docker-compose` (v1) is fine on Raspberry Pi.

### 6. Confirm health

```bash
docker inspect --format='{{.State.Health.Status}}' fleet-resource-monitor
cat data/heartbeat
```

If the process hangs or stops writing heartbeats for ~2 intervals, Docker marks the container unhealthy (`restart: unless-stopped` will not alone fix a wedged PID — check logs / recreate).

## Local development (without Docker)

```bash
cd fleet-resource-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt pytest
export PYTHONPATH=src
export HOSTS_FILE=config/hosts.example.yml
export NTFY_URL=https://ntfy.example.com
export NTFY_TOPIC=fleet-resources
export STATE_FILE=/tmp/fleet-state.json
export HEARTBEAT_FILE=/tmp/fleet-heartbeat
export SSH_IDENTITY_FILE=$HOME/.ssh/id_ed25519
export SSH_KNOWN_HOSTS=$HOME/.ssh/known_hosts

python -m fleet_monitor --once --dry-run -v
pytest -q
```

## Environment reference

| Variable | Default | Meaning |
|---|---|---|
| `NTFY_URL` | _(required)_ | Base URL of ntfy server |
| `NTFY_TOPIC` | _(required)_ | Topic name (example: `fleet-resources`) |
| `NTFY_TOKEN` | _(empty)_ | Optional bearer token |
| `HOSTS_FILE` | `/config/hosts.yml` | Inventory path |
| `STATE_FILE` | `/data/state.json` | Alert state |
| `HEARTBEAT_FILE` | `/data/heartbeat` | Loop liveness stamp |
| `SSH_IDENTITY_FILE` | `/ssh/id_ed25519` | Private key (mounted ro) |
| `SSH_KNOWN_HOSTS` | `/ssh/known_hosts` | known_hosts (mounted ro) |
| `SSH_CONNECT_TIMEOUT` | `10` | SSH `ConnectTimeout` |
| `CHECK_INTERVAL_SECONDS` | `900` | Loop sleep (15m) |
| `ALERT_COOLDOWN_SECONDS` | `21600` | Re-alert while still bad (6h) |
| `DISK_WARN_PERCENT` | `85` | Disk warn threshold |
| `DISK_CRITICAL_PERCENT` | `92` | Disk critical threshold |
| `RAM_CRITICAL_PERCENT` | `90` | RAM critical (MemAvailable) |
| `LOAD_MULTIPLIER` | `2` | `load1` vs `nproc` factor |
| `TEMP_WARN_CELSIUS` | `70` | SoC/CPU temperature warn (°C) |
| `TEMP_CRITICAL_CELSIUS` | `80` | SoC/CPU temperature critical (°C; Pi soft-throttle territory) |
| `DISK_CONSECUTIVE_REQUIRED` | `2` | Consecutive disk warn/critical (and OK) checks |
| `RAM_CONSECUTIVE_REQUIRED` | `2` | Consecutive RAM critical (and OK) checks |
| `LOAD_CONSECUTIVE_REQUIRED` | `2` | Consecutive elevated load (and OK) checks |
| `TEMP_CONSECUTIVE_REQUIRED` | `1` | Consecutive temperature warn/critical (and OK) checks |
| `UNREACHABLE_CONSECUTIVE_REQUIRED` | `2` | Consecutive unreachable (and reachable) checks |

## SSH behaviour

- `BatchMode=yes` (no password prompts)
- Short `ConnectTimeout`
- `StrictHostKeyChecking=yes` + mounted `known_hosts`
- Direct Tailscale SSH by default; optional `ProxyJump` from inventory

Remote collection is a small POSIX shell snippet over SSH stdin (disk via `df -P`, RAM via `/proc/meminfo`, load via `/proc/loadavg`, CPUs via `nproc`, temperature via the hottest readable `/sys/class/thermal/thermal_zone*/temp` and optionally `vcgencmd measure_temp`). If no thermal sensor is available the host check still succeeds; temperature is simply omitted.

## Secrets (do not commit)

`.gitignore` excludes:

- `.env` (copy from `.env.example`)
- `config/hosts.yml` (copy from `config/hosts.example.yml`)
- `ssh/` (private key + known_hosts)
- `data/` (runtime `state.json` / heartbeat)

## Non-goals

- Not Prometheus / Grafana
- Not an Uptime Kuma replacement (no HTTP uptime probes)
- No automatic docker prune or remediation
