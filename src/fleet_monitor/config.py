"""Configuration loading from environment and hosts inventory."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml

from fleet_monitor.models import HostSpec, ThresholdConfig


@dataclass(frozen=True)
class NtfyConfig:
    url: str
    topic: str
    token: str | None = None


@dataclass(frozen=True)
class SshConfig:
    identity_file: Path
    known_hosts: Path
    connect_timeout_seconds: int = 10
    batch_mode: bool = True


@dataclass(frozen=True)
class AppConfig:
    hosts: tuple[HostSpec, ...]
    thresholds: ThresholdConfig
    ntfy: NtfyConfig
    ssh: SshConfig
    state_file: Path
    heartbeat_file: Path
    check_interval_seconds: int
    alert_cooldown_seconds: int
    dry_run: bool = False


def _require_env(name: str, env: Mapping[str, str]) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _env_float(name: str, default: float, env: Mapping[str, str]) -> float:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int, env: Mapping[str, str]) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _env_path(name: str, default: str, env: Mapping[str, str]) -> Path:
    raw = env.get(name, default).strip()
    return Path(raw)


def load_hosts_inventory(path: Path) -> tuple[HostSpec, ...]:
    if not path.is_file():
        raise FileNotFoundError(f"Hosts inventory not found: {path}")

    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)

    if not isinstance(data, dict) or "hosts" not in data:
        raise ValueError("Hosts inventory must be a mapping with a top-level 'hosts' list")

    raw_hosts = data["hosts"]
    if not isinstance(raw_hosts, list) or not raw_hosts:
        raise ValueError("Hosts inventory 'hosts' must be a non-empty list")

    by_name: dict[str, HostSpec] = {}

    for index, item in enumerate(raw_hosts):
        if not isinstance(item, dict):
            raise ValueError(f"Host entry #{index + 1} must be a mapping")

        name = str(item.get("name", "")).strip()
        host = str(item.get("host", "")).strip()
        user = str(item.get("user", "")).strip()
        if not name or not host or not user:
            raise ValueError(f"Host entry #{index + 1} requires name, host, and user")

        local = bool(item.get("local", False)) or host in {"localhost", "127.0.0.1", "::1"}
        extra_raw = item.get("extra_mounts", [])
        if extra_raw is None:
            extra_mounts: tuple[str, ...] = ()
        elif isinstance(extra_raw, list):
            extra_mounts = tuple(str(mount) for mount in extra_raw)
        else:
            raise ValueError(f"Host '{name}': extra_mounts must be a list")

        proxy_raw = item.get("proxy_jump")
        proxy_jump: str | None = None
        if proxy_raw is not None and str(proxy_raw).strip():
            proxy_jump = str(proxy_raw).strip()

        by_name[name] = HostSpec(
            name=name,
            host=host,
            user=user,
            proxy_jump=proxy_jump,
            local=local,
            extra_mounts=extra_mounts,
        )

    # Resolve proxy_jump inventory names to user@host
    resolved: list[HostSpec] = []
    for spec in by_name.values():
        if not spec.proxy_jump:
            resolved.append(spec)
            continue
        jump = spec.proxy_jump
        if "@" in jump:
            resolved.append(spec)
            continue
        if jump not in by_name:
            raise ValueError(
                f"Host '{spec.name}': proxy_jump '{jump}' is not an inventory name "
                "and does not look like user@host"
            )
        jump_host = by_name[jump]
        resolved.append(
            HostSpec(
                name=spec.name,
                host=spec.host,
                user=spec.user,
                proxy_jump=f"{jump_host.user}@{jump_host.host}",
                local=spec.local,
                extra_mounts=spec.extra_mounts,
            )
        )

    return tuple(resolved)


def load_config(
    env: Mapping[str, str] | None = None,
    *,
    dry_run: bool = False,
) -> AppConfig:
    env_map = dict(os.environ if env is None else env)

    hosts_path = _env_path("HOSTS_FILE", "/config/hosts.yml", env_map)
    hosts = load_hosts_inventory(hosts_path)

    ntfy = NtfyConfig(
        url=_require_env("NTFY_URL", env_map).rstrip("/"),
        topic=_require_env("NTFY_TOPIC", env_map),
        token=(env_map.get("NTFY_TOKEN") or "").strip() or None,
    )

    ssh = SshConfig(
        identity_file=_env_path("SSH_IDENTITY_FILE", "/ssh/id_ed25519", env_map),
        known_hosts=_env_path("SSH_KNOWN_HOSTS", "/ssh/known_hosts", env_map),
        connect_timeout_seconds=_env_int("SSH_CONNECT_TIMEOUT", 10, env_map),
    )

    thresholds = ThresholdConfig(
        disk_warn_percent=_env_float("DISK_WARN_PERCENT", 85.0, env_map),
        disk_critical_percent=_env_float("DISK_CRITICAL_PERCENT", 92.0, env_map),
        ram_critical_percent=_env_float("RAM_CRITICAL_PERCENT", 90.0, env_map),
        load_multiplier=_env_float("LOAD_MULTIPLIER", 2.0, env_map),
        temp_warn_celsius=_env_float("TEMP_WARN_CELSIUS", 70.0, env_map),
        temp_critical_celsius=_env_float("TEMP_CRITICAL_CELSIUS", 80.0, env_map),
        load_consecutive_required=_env_int("LOAD_CONSECUTIVE_REQUIRED", 2, env_map),
        disk_consecutive_required=_env_int("DISK_CONSECUTIVE_REQUIRED", 2, env_map),
        ram_consecutive_required=_env_int("RAM_CONSECUTIVE_REQUIRED", 2, env_map),
        temp_consecutive_required=_env_int("TEMP_CONSECUTIVE_REQUIRED", 1, env_map),
        unreachable_consecutive_required=_env_int(
            "UNREACHABLE_CONSECUTIVE_REQUIRED", 2, env_map
        ),
    )

    return AppConfig(
        hosts=hosts,
        thresholds=thresholds,
        ntfy=ntfy,
        ssh=ssh,
        state_file=_env_path("STATE_FILE", "/data/state.json", env_map),
        heartbeat_file=_env_path("HEARTBEAT_FILE", "/data/heartbeat", env_map),
        check_interval_seconds=_env_int("CHECK_INTERVAL_SECONDS", 900, env_map),
        alert_cooldown_seconds=_env_int("ALERT_COOLDOWN_SECONDS", 21600, env_map),
        dry_run=dry_run,
    )
