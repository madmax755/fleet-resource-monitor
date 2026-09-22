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
    disk_consecutive_required: int = 2
    ram_consecutive_required: int = 2
    unreachable_consecutive_required: int = 2


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


# Large streak seeded for legacy state so already-confirmed conditions do not
# re-alert or require re-confirmation just because we added consecutive fields.
_LEGACY_CONFIRMED_STREAK = 1_000_000


@dataclass
class ConditionState:
    """Persisted state for one host+condition key."""

    severity: str
    consecutive_load_high: int = 0  # legacy; mirrored for load keys
    last_alert_unix: float = 0.0
    last_seen_unix: float = 0.0
    message: str = ""
    candidate_severity: str = ""
    consecutive_count: int = 0

    def resolved_candidate(self) -> str:
        return self.candidate_severity or self.severity


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
                    "candidate_severity": value.resolved_candidate(),
                    "consecutive_count": value.consecutive_count,
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
                severity = str(value.get("severity", Severity.OK.value))
                consecutive_load_high = int(value.get("consecutive_load_high", 0))
                has_new_fields = (
                    "candidate_severity" in value or "consecutive_count" in value
                )
                if has_new_fields:
                    candidate = str(value.get("candidate_severity", "") or severity)
                    consecutive_count = int(value.get("consecutive_count", 0))
                else:
                    # Migrate legacy state without re-alerting confirmed conditions.
                    # Preserve in-progress load streaks that had not yet alerted.
                    if (
                        str(key).endswith("|load")
                        and consecutive_load_high > 0
                        and severity == Severity.OK.value
                    ):
                        candidate = Severity.CRITICAL.value
                        consecutive_count = consecutive_load_high
                    else:
                        candidate = severity
                        consecutive_count = _LEGACY_CONFIRMED_STREAK

                conditions[str(key)] = ConditionState(
                    severity=severity,
                    consecutive_load_high=consecutive_load_high,
                    last_alert_unix=float(value.get("last_alert_unix", 0.0)),
                    last_seen_unix=float(value.get("last_seen_unix", 0.0)),
                    message=str(value.get("message", "")),
                    candidate_severity=candidate,
                    consecutive_count=consecutive_count,
                )
        return cls(
            conditions=conditions,
            last_successful_loop_unix=float(data.get("last_successful_loop_unix", 0.0)),
            last_checker_alert_unix=float(data.get("last_checker_alert_unix", 0.0)),
        )
