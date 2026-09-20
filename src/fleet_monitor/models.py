"""Typed models for hosts, metrics, alerts, and check outcomes."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping


class Severity(str, Enum):
    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"


class AlertKind(str, Enum):
    DISK = "disk"
    RAM = "ram"
    LOAD = "load"
    HOST_UNREACHABLE = "host_unreachable"
    CHECKER_FAILURE = "checker_failure"
    RECOVERY = "recovery"


@dataclass(frozen=True)
class HostSpec:
    name: str
    host: str
    user: str
    proxy_jump: str | None = None
    local: bool = False
    extra_mounts: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiskUsage:
    mount: str
    used_percent: float


@dataclass(frozen=True)
class HostMetrics:
    host_name: str
    disks: tuple[DiskUsage, ...]
    mem_available_kb: int
    mem_total_kb: int
    load1: float
    nproc: int

    @property
    def ram_used_percent(self) -> float:
        if self.mem_total_kb <= 0:
            return 100.0
        used = self.mem_total_kb - self.mem_available_kb
        return max(0.0, min(100.0, (used / self.mem_total_kb) * 100.0))


@dataclass(frozen=True)
class ThresholdConfig:
    disk_warn_percent: float = 85.0
    disk_critical_percent: float = 92.0
    ram_critical_percent: float = 90.0
    load_multiplier: float = 2.0
    load_consecutive_required: int = 2


@dataclass(frozen=True)
class AlertEvent:
    host_name: str
    kind: AlertKind
    severity: Severity
    message: str
    detail: str = ""
    recovered: bool = False


@dataclass
class Finding:
    """A single threshold finding for a host/metric key."""

    key: str
    kind: AlertKind
    severity: Severity
    message: str
    detail: str = ""


@dataclass
class HostCheckResult:
    host_name: str
    ok: bool
    metrics: HostMetrics | None = None
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None


@dataclass
class ConditionState:
    """Persisted state for one host+condition key."""

    severity: str
    consecutive_load_high: int = 0
    last_alert_unix: float = 0.0
    last_seen_unix: float = 0.0
    message: str = ""


@dataclass
class MonitorState:
    conditions: dict[str, ConditionState] = field(default_factory=dict)
    last_successful_loop_unix: float = 0.0
    last_checker_alert_unix: float = 0.0

    def to_dict(self) -> dict[str, object]:
        return {
            "conditions": {
                key: {
                    "severity": value.severity,
                    "consecutive_load_high": value.consecutive_load_high,
                    "last_alert_unix": value.last_alert_unix,
                    "last_seen_unix": value.last_seen_unix,
                    "message": value.message,
                }
                for key, value in self.conditions.items()
            },
            "last_successful_loop_unix": self.last_successful_loop_unix,
            "last_checker_alert_unix": self.last_checker_alert_unix,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> MonitorState:
        raw_conditions = data.get("conditions", {})
        conditions: dict[str, ConditionState] = {}
        if isinstance(raw_conditions, Mapping):
            for key, value in raw_conditions.items():
                if not isinstance(value, Mapping):
                    continue
                conditions[str(key)] = ConditionState(
                    severity=str(value.get("severity", Severity.OK.value)),
                    consecutive_load_high=int(value.get("consecutive_load_high", 0)),
                    last_alert_unix=float(value.get("last_alert_unix", 0.0)),
                    last_seen_unix=float(value.get("last_seen_unix", 0.0)),
                    message=str(value.get("message", "")),
                )
        return cls(
            conditions=conditions,
            last_successful_loop_unix=float(data.get("last_successful_loop_unix", 0.0)),
            last_checker_alert_unix=float(data.get("last_checker_alert_unix", 0.0)),
        )
