"""Threshold evaluation for disk, RAM, and load."""

from __future__ import annotations

from fleet_monitor.models import (
    AlertKind,
    Finding,
    HostMetrics,
    Severity,
    ThresholdConfig,
)


def evaluate_disk(metrics: HostMetrics, thresholds: ThresholdConfig) -> list[Finding]:
    findings: list[Finding] = []
    for disk in metrics.disks:
        if disk.used_percent >= thresholds.disk_critical_percent:
            severity = Severity.CRITICAL
        elif disk.used_percent >= thresholds.disk_warn_percent:
            severity = Severity.WARN
        else:
            continue
        findings.append(
            Finding(
                key=f"disk:{disk.mount}",
                kind=AlertKind.DISK,
                severity=severity,
                message=(
                    f"{metrics.host_name} disk {disk.mount} at "
                    f"{disk.used_percent:.0f}% used"
                ),
                detail=(
                    f"warn>={thresholds.disk_warn_percent:.0f}% "
                    f"critical>={thresholds.disk_critical_percent:.0f}%"
                ),
            )
        )
    return findings


def evaluate_ram(metrics: HostMetrics, thresholds: ThresholdConfig) -> list[Finding]:
    used = metrics.ram_used_percent
    if used < thresholds.ram_critical_percent:
        return []
    return [
        Finding(
            key="ram",
            kind=AlertKind.RAM,
            severity=Severity.CRITICAL,
            message=f"{metrics.host_name} RAM at {used:.0f}% used",
            detail=(
                f"MemAvailable-based; critical>={thresholds.ram_critical_percent:.0f}% "
                f"({metrics.mem_available_kb}KB available / {metrics.mem_total_kb}KB total)"
            ),
        )
    ]


def is_load_elevated(metrics: HostMetrics, thresholds: ThresholdConfig) -> bool:
    limit = thresholds.load_multiplier * metrics.nproc
    return metrics.load1 >= limit


def evaluate_load_after_consecutive(
    metrics: HostMetrics,
    thresholds: ThresholdConfig,
    consecutive_high: int,
) -> Finding | None:
    """Return a load finding only once consecutive_high meets the required streak."""
    if consecutive_high < thresholds.load_consecutive_required:
        return None
    limit = thresholds.load_multiplier * metrics.nproc
    return Finding(
        key="load",
        kind=AlertKind.LOAD,
        severity=Severity.CRITICAL,
        message=(
            f"{metrics.host_name} load1={metrics.load1:.2f} "
            f">= {thresholds.load_multiplier:.0f}×nproc ({limit:.2f}) "
            f"for {consecutive_high} consecutive checks"
        ),
        detail=f"nproc={metrics.nproc}",
    )


def evaluate_static_findings(
    metrics: HostMetrics,
    thresholds: ThresholdConfig,
) -> list[Finding]:
    """Disk + RAM findings (load is handled with consecutive state)."""
    return [
        *evaluate_disk(metrics, thresholds),
        *evaluate_ram(metrics, thresholds),
    ]
