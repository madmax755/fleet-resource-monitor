"""Tests for threshold evaluation."""

from __future__ import annotations

from fleet_monitor.models import AlertKind, DiskUsage, HostMetrics, Severity, ThresholdConfig
from fleet_monitor.thresholds import (
    consecutive_required_for,
    evaluate_disk,
    evaluate_load,
    evaluate_load_after_consecutive,
    evaluate_ram,
    evaluate_static_findings,
    evaluate_temp,
    is_load_elevated,
)


def _metrics(
    *,
    disk_pct: float = 50.0,
    ram_available_kb: int = 5_000_000,
    ram_total_kb: int = 10_000_000,
    load1: float = 0.5,
    nproc: int = 4,
    temp_celsius: float | None = None,
) -> HostMetrics:
    return HostMetrics(
        host_name="testbox",
        disks=(DiskUsage(mount="/", used_percent=disk_pct),),
        mem_available_kb=ram_available_kb,
        mem_total_kb=ram_total_kb,
        load1=load1,
        nproc=nproc,
        temp_celsius=temp_celsius,
    )


def test_disk_warn_and_critical() -> None:
    thresholds = ThresholdConfig()
    warn = evaluate_disk(_metrics(disk_pct=85.0), thresholds)
    assert len(warn) == 1
    assert warn[0].severity is Severity.WARN

    critical = evaluate_disk(_metrics(disk_pct=92.0), thresholds)
    assert len(critical) == 1
    assert critical[0].severity is Severity.CRITICAL

    ok = evaluate_disk(_metrics(disk_pct=84.9), thresholds)
    assert ok == []


def test_ram_uses_mem_available() -> None:
    thresholds = ThresholdConfig(ram_critical_percent=90.0)
    # 10% available => 90% used
    findings = evaluate_ram(
        _metrics(ram_available_kb=1_000_000, ram_total_kb=10_000_000),
        thresholds,
    )
    assert len(findings) == 1
    assert findings[0].severity is Severity.CRITICAL

    ok = evaluate_ram(
        _metrics(ram_available_kb=2_000_000, ram_total_kb=10_000_000),
        thresholds,
    )
    assert ok == []


def test_temp_warn_and_critical() -> None:
    thresholds = ThresholdConfig(temp_warn_celsius=70.0, temp_critical_celsius=80.0)
    warn = evaluate_temp(_metrics(temp_celsius=70.0), thresholds)
    assert len(warn) == 1
    assert warn[0].kind is AlertKind.TEMP
    assert warn[0].key == "temp"
    assert warn[0].severity is Severity.WARN

    critical = evaluate_temp(_metrics(temp_celsius=80.0), thresholds)
    assert len(critical) == 1
    assert critical[0].severity is Severity.CRITICAL

    ok = evaluate_temp(_metrics(temp_celsius=69.9), thresholds)
    assert ok == []


def test_temp_absent_sensor_emits_no_finding() -> None:
    thresholds = ThresholdConfig()
    assert evaluate_temp(_metrics(temp_celsius=None), thresholds) == []
    static = evaluate_static_findings(_metrics(temp_celsius=None), thresholds)
    assert all(f.kind is not AlertKind.TEMP for f in static)


def test_load_requires_consecutive_checks() -> None:
    thresholds = ThresholdConfig(load_multiplier=2.0, load_consecutive_required=2)
    metrics = _metrics(load1=8.0, nproc=4)  # 2× nproc == 8
    assert is_load_elevated(metrics, thresholds) is True
    assert evaluate_load_after_consecutive(metrics, thresholds, 1) is None
    finding = evaluate_load_after_consecutive(metrics, thresholds, 2)
    assert finding is not None
    assert finding.severity is Severity.CRITICAL


def test_static_findings_combine_disk_and_ram() -> None:
    thresholds = ThresholdConfig()
    findings = evaluate_static_findings(
        _metrics(
            disk_pct=93.0,
            ram_available_kb=500_000,
            ram_total_kb=10_000_000,
        ),
        thresholds,
    )
    kinds = {f.kind.value for f in findings}
    assert kinds == {"disk", "ram"}


def test_static_findings_include_temp_when_hot() -> None:
    thresholds = ThresholdConfig()
    findings = evaluate_static_findings(
        _metrics(temp_celsius=85.0),
        thresholds,
    )
    temp = [f for f in findings if f.kind is AlertKind.TEMP]
    assert len(temp) == 1
    assert temp[0].severity is Severity.CRITICAL


def test_evaluate_load_without_streak() -> None:
    thresholds = ThresholdConfig(load_multiplier=2.0)
    metrics = _metrics(load1=8.0, nproc=4)
    finding = evaluate_load(metrics, thresholds)
    assert finding is not None
    assert finding.key == "load"
    assert evaluate_load(_metrics(load1=1.0, nproc=4), thresholds) is None


def test_consecutive_required_for_keys() -> None:
    thresholds = ThresholdConfig(
        disk_consecutive_required=2,
        ram_consecutive_required=3,
        load_consecutive_required=4,
        temp_consecutive_required=1,
        unreachable_consecutive_required=5,
    )
    assert consecutive_required_for("disk:/", thresholds) == 2
    assert consecutive_required_for("ram", thresholds) == 3
    assert consecutive_required_for("load", thresholds) == 4
    assert consecutive_required_for("temp", thresholds) == 1
    assert consecutive_required_for("unreachable", thresholds) == 5


def test_temp_consecutive_default_is_one() -> None:
    thresholds = ThresholdConfig()
    assert thresholds.temp_consecutive_required == 1
    assert consecutive_required_for("temp", thresholds) == 1
