"""SSH and local metric collection for fleet hosts."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
from typing import Sequence

from fleet_monitor.config import SshConfig
from fleet_monitor.models import DiskUsage, HostMetrics, HostSpec

LOGGER = logging.getLogger(__name__)

REMOTE_COLLECT_SCRIPT = r"""
set -eu
mounts="${MOUNTS:-/}"
for mount in $mounts; do
  line="$(df -P "$mount" 2>/dev/null | awk 'NR==2 {print $5}')"
  if [ -n "${line:-}" ]; then
    pct="$(printf '%s' "$line" | tr -d '%')"
    printf 'DISK\t%s\t%s\n' "$mount" "$pct"
  else
    printf 'DISK_ERR\t%s\n' "$mount"
  fi
done
awk '
  /^MemTotal:/ { total=$2 }
  /^MemAvailable:/ { available=$2 }
  END {
    if (total == "" || available == "") {
      print "MEM_ERR"
      exit 0
    }
    printf "MEM\t%s\t%s\n", available, total
  }
' /proc/meminfo
load1="$(awk '{print $1}' /proc/loadavg)"
printf 'LOAD\t%s\n' "$load1"
if command -v nproc >/dev/null 2>&1; then
  nproc_val="$(nproc)"
else
  nproc_val="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 1)"
fi
printf 'NPROC\t%s\n' "$nproc_val"
# Optional SoC/CPU temperature: hottest thermal_zone (millidegrees C),
# plus vcgencmd on Raspberry Pi when present. Skip silently if none.
max_mc=""
for zone_temp in /sys/class/thermal/thermal_zone*/temp; do
  [ -r "$zone_temp" ] || continue
  val="$(cat "$zone_temp" 2>/dev/null || true)"
  case "$val" in
    ''|*[!0-9]*) continue ;;
  esac
  if [ -z "$max_mc" ] || [ "$val" -gt "$max_mc" ]; then
    max_mc="$val"
  fi
done
max_c=""
if [ -n "$max_mc" ]; then
  max_c="$(awk -v m="$max_mc" 'BEGIN { printf "%.3f", m/1000.0 }')"
fi
if command -v vcgencmd >/dev/null 2>&1; then
  vc_out="$(vcgencmd measure_temp 2>/dev/null || true)"
  vc_c="$(printf '%s' "$vc_out" | sed -n "s/^temp=\\([0-9.][0-9.]*\\).*/\\1/p")"
  if [ -n "$vc_c" ]; then
    if [ -z "$max_c" ]; then
      max_c="$vc_c"
    else
      max_c="$(awk -v a="$max_c" -v b="$vc_c" 'BEGIN { printf "%.3f", (a > b) ? a : b }')"
    fi
  fi
fi
if [ -n "$max_c" ]; then
  printf 'TEMP\t%s\n' "$max_c"
fi
"""


class MetricCollectionError(Exception):
    """Raised when metrics cannot be collected from a host."""


def build_remote_command(mounts: Sequence[str]) -> str:
    mount_list = " ".join(shlex.quote(mount) for mount in mounts)
    return f"MOUNTS={shlex.quote(mount_list)} sh -s"


def build_ssh_argv(host: HostSpec, ssh: SshConfig, remote_command: str) -> list[str]:
    argv = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        f"ConnectTimeout={ssh.connect_timeout_seconds}",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        f"UserKnownHostsFile={ssh.known_hosts}",
        "-i",
        str(ssh.identity_file),
    ]
    if host.proxy_jump:
        argv.extend(["-o", f"ProxyJump={host.proxy_jump}"])
    argv.append(f"{host.user}@{host.host}")
    argv.append(remote_command)
    return argv


def parse_metrics_output(host_name: str, output: str) -> HostMetrics:
    disks: list[DiskUsage] = []
    mem_available_kb: int | None = None
    mem_total_kb: int | None = None
    load1: float | None = None
    nproc: int | None = None
    temp_celsius: float | None = None
    errors: list[str] = []

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("\t")
        kind = parts[0]
        if kind == "DISK" and len(parts) == 3:
            disks.append(DiskUsage(mount=parts[1], used_percent=float(parts[2])))
        elif kind == "DISK_ERR":
            errors.append(f"disk lookup failed for {parts[1] if len(parts) > 1 else '?'}")
        elif kind == "MEM" and len(parts) == 3:
            mem_available_kb = int(parts[1])
            mem_total_kb = int(parts[2])
        elif kind == "MEM_ERR":
            errors.append("MemAvailable/MemTotal missing from /proc/meminfo")
        elif kind == "LOAD" and len(parts) == 2:
            load1 = float(parts[1])
        elif kind == "NPROC" and len(parts) == 2:
            nproc = int(parts[1])
        elif kind == "TEMP" and len(parts) == 2:
            # Optional: absent TEMP line means no sensor (e.g. Proxmox LXC).
            temp_celsius = float(parts[1])
        else:
            errors.append(f"unrecognised metric line: {line}")

    if mem_available_kb is None or mem_total_kb is None:
        errors.append("memory metrics missing")
    if load1 is None:
        errors.append("load metrics missing")
    if nproc is None:
        errors.append("nproc missing")
    if not disks:
        errors.append("no disk metrics collected")
    if errors:
        raise MetricCollectionError("; ".join(errors))

    assert mem_available_kb is not None
    assert mem_total_kb is not None
    assert load1 is not None
    assert nproc is not None

    return HostMetrics(
        host_name=host_name,
        disks=tuple(disks),
        mem_available_kb=mem_available_kb,
        mem_total_kb=mem_total_kb,
        load1=load1,
        nproc=nproc,
        temp_celsius=temp_celsius,
    )


def _run_local(mounts: Sequence[str], timeout: int) -> str:
    env = {"MOUNTS": " ".join(mounts)}
    completed = subprocess.run(
        ["sh", "-s"],
        input=REMOTE_COLLECT_SCRIPT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env={**os.environ, **env},
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "no stderr"
        raise MetricCollectionError(f"local collector exited {completed.returncode}: {stderr}")
    return completed.stdout


def _run_ssh(host: HostSpec, ssh: SshConfig, mounts: Sequence[str]) -> str:
    if not ssh.identity_file.is_file():
        raise MetricCollectionError(f"SSH identity not found: {ssh.identity_file}")
    if not ssh.known_hosts.is_file():
        raise MetricCollectionError(f"SSH known_hosts not found: {ssh.known_hosts}")

    remote_command = build_remote_command(mounts)
    argv = build_ssh_argv(host, ssh, remote_command)
    LOGGER.debug("SSH argv for %s: %s", host.name, argv)
    completed = subprocess.run(
        argv,
        input=REMOTE_COLLECT_SCRIPT,
        capture_output=True,
        text=True,
        timeout=ssh.connect_timeout_seconds + 30,
        check=False,
    )
    if completed.returncode != 0:
        stderr = completed.stderr.strip() or "no stderr"
        raise MetricCollectionError(
            f"ssh to {host.user}@{host.host} failed ({completed.returncode}): {stderr}"
        )
    return completed.stdout


def collect_host_metrics(host: HostSpec, ssh: SshConfig) -> HostMetrics:
    mounts = ("/", *host.extra_mounts)
    timeout = ssh.connect_timeout_seconds + 30
    if host.local:
        output = _run_local(mounts, timeout=timeout)
    else:
        output = _run_ssh(host, ssh, mounts)
    return parse_metrics_output(host.name, output)
