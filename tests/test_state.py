"""Tests for alert state transitions, consecutive confirmation, and cooldown."""

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
        consecutive_required=1,
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
        consecutive_required=1,
    )
    assert second == []

    # After cooldown → re-alert while still critical
    third = apply_findings(
        state,
        "box",
        [finding],
        now_unix=1_000.0 + 21_600,
        cooldown_seconds=21_600,
        consecutive_required=1,
    )
    assert len(third) == 1

    # Recovery
    recovery = apply_findings(
        state,
        "box",
        [],
        now_unix=1_000.0 + 21_700,
        cooldown_seconds=21_600,
        consecutive_required=1,
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
    assert (
        len(
            apply_findings(
                state, "box", [warn], now_unix=1.0, cooldown_seconds=100, consecutive_required=1
            )
        )
        == 1
    )
    escalated = apply_findings(
        state, "box", [critical], now_unix=2.0, cooldown_seconds=100, consecutive_required=1
    )
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
        consecutive_required=1,
    )
    assert len(down) == 1
    assert down[0].kind is AlertKind.HOST_UNREACHABLE

    quiet = apply_host_unreachable(
        state,
        "box",
        "ssh timed out",
        now_unix=20.0,
        cooldown_seconds=100,
        consecutive_required=1,
    )
    assert quiet == []

    up = apply_host_reachable_again(
        state, "box", now_unix=30.0, consecutive_required=1
    )
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
    assert loaded.conditions["box|load"].candidate_severity == "critical"
    assert loaded.conditions["box|load"].consecutive_count == 1


def test_consecutive_required_delays_alert_and_recovery() -> None:
    state = MonitorState()
    finding = Finding(
        key="disk:/",
        kind=AlertKind.DISK,
        severity=Severity.WARN,
        message="warn",
    )

    # First unhealthy observation — pending, no alert
    first = apply_findings(
        state,
        "box",
        [finding],
        now_unix=1.0,
        cooldown_seconds=100,
        consecutive_required=2,
    )
    assert first == []
    cond = state.conditions["box|disk:/"]
    assert cond.severity == Severity.OK.value
    assert cond.candidate_severity == Severity.WARN.value
    assert cond.consecutive_count == 1

    # Second consecutive — confirm and alert
    second = apply_findings(
        state,
        "box",
        [finding],
        now_unix=2.0,
        cooldown_seconds=100,
        consecutive_required=2,
    )
    assert len(second) == 1
    assert second[0].severity is Severity.WARN
    assert state.conditions["box|disk:/"].severity == Severity.WARN.value

    # Single OK — not enough to recover
    almost = apply_findings(
        state,
        "box",
        [],
        now_unix=3.0,
        cooldown_seconds=100,
        consecutive_required=2,
    )
    assert almost == []
    assert state.conditions["box|disk:/"].severity == Severity.WARN.value
    assert state.conditions["box|disk:/"].candidate_severity == Severity.OK.value
    assert state.conditions["box|disk:/"].consecutive_count == 1

    # Brief return to warn resets OK streak (flap killed, no extra alert)
    flap = apply_findings(
        state,
        "box",
        [finding],
        now_unix=4.0,
        cooldown_seconds=100,
        consecutive_required=2,
    )
    assert flap == []
    assert state.conditions["box|disk:/"].severity == Severity.WARN.value

    # Two OKs — recovery
    apply_findings(
        state, "box", [], now_unix=5.0, cooldown_seconds=100, consecutive_required=2
    )
    recovered = apply_findings(
        state, "box", [], now_unix=6.0, cooldown_seconds=100, consecutive_required=2
    )
    assert len(recovered) == 1
    assert recovered[0].recovered is True
    assert state.conditions["box|disk:/"].severity == Severity.OK.value


def test_unreachable_requires_consecutive_both_ways() -> None:
    state = MonitorState()
    assert (
        apply_host_unreachable(
            state,
            "box",
            "timeout",
            now_unix=1.0,
            cooldown_seconds=100,
            consecutive_required=2,
        )
        == []
    )
    down = apply_host_unreachable(
        state,
        "box",
        "timeout",
        now_unix=2.0,
        cooldown_seconds=100,
        consecutive_required=2,
    )
    assert len(down) == 1

    assert (
        apply_host_reachable_again(
            state, "box", now_unix=3.0, consecutive_required=2
        )
        == []
    )
    assert state.conditions["box|unreachable"].severity == Severity.CRITICAL.value

    up = apply_host_reachable_again(
        state, "box", now_unix=4.0, consecutive_required=2
    )
    assert len(up) == 1
    assert up[0].recovered is True


def test_legacy_state_migration_preserves_confirmed(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(
        """
{
  "conditions": {
    "box|disk:/": {
      "severity": "critical",
      "consecutive_load_high": 0,
      "last_alert_unix": 100.0,
      "last_seen_unix": 100.0,
      "message": "old critical"
    },
    "box|load": {
      "severity": "ok",
      "consecutive_load_high": 1,
      "last_alert_unix": 0.0,
      "last_seen_unix": 50.0,
      "message": ""
    }
  },
  "last_successful_loop_unix": 100.0,
  "last_checker_alert_unix": 0.0
}
""",
        encoding="utf-8",
    )
    loaded = load_state(path)
    disk = loaded.conditions["box|disk:/"]
    assert disk.severity == "critical"
    assert disk.candidate_severity == "critical"
    assert disk.consecutive_count >= 2

    load = loaded.conditions["box|load"]
    assert load.severity == "ok"
    assert load.candidate_severity == "critical"
    assert load.consecutive_count == 1

    # Already-confirmed disk must not re-alert on the next critical observation
    finding = Finding(
        key="disk:/",
        kind=AlertKind.DISK,
        severity=Severity.CRITICAL,
        message="still critical",
    )
    alerts = apply_findings(
        loaded,
        "box",
        [finding],
        now_unix=160.0,
        cooldown_seconds=21_600,
        consecutive_required=2,
    )
    assert alerts == []
