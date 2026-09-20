"""Tests for alert state transitions and cooldown."""

from __future__ import annotations

from pathlib import Path

from fleet_monitor.models import AlertKind, Finding, MonitorState, Severity
from fleet_monitor.state import (
    apply_findings,
    apply_host_reachable_again,
    apply_host_unreachable,
    bump_load_streak,
    load_state,
    save_state,
)


def test_alert_on_state_change_and_cooldown(tmp_path: Path) -> None:
    state = MonitorState()
    finding = Finding(
        key="disk:/",
        kind=AlertKind.DISK,
        severity=Severity.CRITICAL,
        message="box disk / at 95%",
    )
    first = apply_findings(
        state,
        "box",
        [finding],
        now_unix=1_000.0,
        cooldown_seconds=21_600,
    )
    assert len(first) == 1
    assert first[0].severity is Severity.CRITICAL

    # Same severity within cooldown → no re-alert
    second = apply_findings(
        state,
        "box",
        [finding],
        now_unix=1_000.0 + 60,
        cooldown_seconds=21_600,
    )
    assert second == []

    # After cooldown → re-alert while still critical
    third = apply_findings(
        state,
        "box",
        [finding],
        now_unix=1_000.0 + 21_600,
        cooldown_seconds=21_600,
    )
    assert len(third) == 1

    # Recovery
    recovery = apply_findings(
        state,
        "box",
        [],
        now_unix=1_000.0 + 21_700,
        cooldown_seconds=21_600,
    )
    assert len(recovery) == 1
    assert recovery[0].recovered is True
    assert recovery[0].kind is AlertKind.RECOVERY


def test_warn_to_critical_is_state_change() -> None:
    state = MonitorState()
    warn = Finding(
        key="disk:/",
        kind=AlertKind.DISK,
        severity=Severity.WARN,
        message="warn",
    )
    critical = Finding(
        key="disk:/",
        kind=AlertKind.DISK,
        severity=Severity.CRITICAL,
        message="critical",
    )
    assert len(apply_findings(state, "box", [warn], now_unix=1.0, cooldown_seconds=100)) == 1
    escalated = apply_findings(state, "box", [critical], now_unix=2.0, cooldown_seconds=100)
    assert len(escalated) == 1
    assert escalated[0].severity is Severity.CRITICAL


def test_unreachable_and_recovery() -> None:
    state = MonitorState()
    down = apply_host_unreachable(
        state,
        "box",
        "ssh timed out",
        now_unix=10.0,
        cooldown_seconds=100,
    )
    assert len(down) == 1
    assert down[0].kind is AlertKind.HOST_UNREACHABLE

    quiet = apply_host_unreachable(
        state,
        "box",
        "ssh timed out",
        now_unix=20.0,
        cooldown_seconds=100,
    )
    assert quiet == []

    up = apply_host_reachable_again(state, "box", now_unix=30.0)
    assert len(up) == 1
    assert up[0].recovered is True


def test_load_streak_helper() -> None:
    state = MonitorState()
    assert bump_load_streak(state, "box", True) == 1
    assert bump_load_streak(state, "box", True) == 2
    assert bump_load_streak(state, "box", False) == 0


def test_state_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    state = MonitorState(last_successful_loop_unix=42.0)
    bump_load_streak(state, "box", True)
    save_state(path, state)
    loaded = load_state(path)
    assert loaded.last_successful_loop_unix == 42.0
    assert loaded.conditions["box|load"].consecutive_load_high == 1
