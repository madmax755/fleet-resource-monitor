"""Threshold evaluation for disk, RAM, load, and temperature."""

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


def evaluate_temp(metrics: HostMetrics, thresholds: ThresholdConfig) -> list[Finding]:
    """Emit a temp finding when a sensor reading crosses warn/critical thresholds.

    Hosts with no readable thermal sensor (``temp_celsius is None``) produce no
    finding — common on Proxmox LXCs — and must not invent an error.
    """
    temp = metrics.temp_celsius
    if temp is None:
        return []
    if temp >= thresholds.temp_critical_celsius:
        severity = Severity.CRITICAL
    elif temp >= thresholds.temp_warn_celsius:
        severity = Severity.WARN
    else:
        return []
    return [
        Finding(
            key="temp",
            kind=AlertKind.TEMP,
            severity=severity,
            message=f"{metrics.host_name} temperature at {temp:.1f}°C",
            detail=(
                f"warn>={thresholds.temp_warn_celsius:.0f}°C "
                f"critical>={thresholds.temp_critical_celsius:.0f}°C"
            ),
        )
    ]


def is_load_elevated(metrics: HostMetrics, thresholds: ThresholdConfig) -> bool:
    limit = thresholds.load_multiplier * metrics.nproc
    return metrics.load1 >= limit


def evaluate_load(metrics: HostMetrics, thresholds: ThresholdConfig) -> Finding | None:
    """Return a load finding when load1 is elevated (confirmation is in state)."""
    if not is_load_elevated(metrics, thresholds):
        return None
    limit = thresholds.load_multiplier * metrics.nproc
    return Finding(
        key="load",
        kind=AlertKind.LOAD,
        severity=Severity.CRITICAL,
        message=(
            f"{metrics.host_name} load1={metrics.load1:.2f} "
            f">= {thresholds.load_multiplier:.0f}×nproc ({limit:.2f})"
        ),
        detail=f"nproc={metrics.nproc}",
    )


def evaluate_load_after_consecutive(
    metrics: HostMetrics,
    thresholds: ThresholdConfig,
    consecutive_high: int,
) -> Finding | None:
    """Compatibility helper: only emit once consecutive_high meets the required streak."""
    if consecutive_high < thresholds.load_consecutive_required:
        return None
    finding = evaluate_load(metrics, thresholds)
    if finding is None:
        return None
    return Finding(
        key=finding.key,
        kind=finding.kind,
        severity=finding.severity,
        message=(
            f"{finding.message} for {consecutive_high} consecutive checks"
        ),
        detail=finding.detail,
    )


def evaluate_static_findings(
    metrics: HostMetrics,
    thresholds: ThresholdConfig,
) -> list[Finding]:
    """Disk + RAM + temperature findings (load is evaluated separately)."""
    return [
        *evaluate_disk(metrics, thresholds),
        *evaluate_ram(metrics, thresholds),
        *evaluate_temp(metrics, thresholds),
    ]


def consecutive_required_for(
    finding_key: str,
    thresholds: ThresholdConfig,
) -> int:
    """Map a finding key to its consecutive-confirmation requirement."""
    if finding_key.startswith("disk:"):
        return thresholds.disk_consecutive_required
    if finding_key == "ram":
        return thresholds.ram_consecutive_required
    if finding_key == "load":
        return thresholds.load_consecutive_required
    if finding_key == "temp":
        return thresholds.temp_consecutive_required
    if finding_key == "unreachable":
        return thresholds.unreachable_consecutive_required
    return 2
