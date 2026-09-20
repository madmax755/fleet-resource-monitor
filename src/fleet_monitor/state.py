"""Persistent alert state with change detection and cooldown re-alerts."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fleet_monitor.models import (
    AlertEvent,
    AlertKind,
    ConditionState,
    Finding,
    MonitorState,
    Severity,
)

LOGGER = logging.getLogger(__name__)


def condition_key(host_name: str, finding_key: str) -> str:
    return f"{host_name}|{finding_key}"


def load_state(path: Path) -> MonitorState:
    if not path.is_file():
        return MonitorState()
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, dict):
            LOGGER.warning("State file %s is not a JSON object; starting fresh", path)
            return MonitorState()
        return MonitorState.from_dict(data)
    except (OSError, json.JSONDecodeError) as exc:
        LOGGER.warning("Failed to load state from %s (%s); starting fresh", path, exc)
        return MonitorState()


def save_state(path: Path, state: MonitorState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(state.to_dict(), handle, indent=2, sort_keys=True)
        handle.write("\n")
    tmp_path.replace(path)


def write_heartbeat(path: Path, unix_ts: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{unix_ts:.3f}\n", encoding="utf-8")


def apply_findings(
    state: MonitorState,
    host_name: str,
    findings: list[Finding],
    *,
    now_unix: float,
    cooldown_seconds: int,
) -> list[AlertEvent]:
    """
    Update state for a reachable host and decide which alerts to emit.

    Alert policy:
    - Emit immediately on severity/state change (including recovery to OK).
    - While still unhealthy, re-alert at most once per cooldown window
      (default 6h) so critical hosts are not silent forever, but do not spam
      every 15-minute loop.
    """
    alerts: list[AlertEvent] = []
    active_keys = {condition_key(host_name, finding.key) for finding in findings}

    for finding in findings:
        key = condition_key(host_name, finding.key)
        previous = state.conditions.get(key)
        previous_severity = (
            Severity(previous.severity) if previous is not None else Severity.OK
        )
        should_alert = False
        if previous is None or previous_severity != finding.severity:
            should_alert = True
        elif (
            finding.severity != Severity.OK
            and now_unix - previous.last_alert_unix >= cooldown_seconds
        ):
            should_alert = True

        if should_alert:
            alerts.append(
                AlertEvent(
                    host_name=host_name,
                    kind=finding.kind,
                    severity=finding.severity,
                    message=finding.message,
                    detail=finding.detail,
                    recovered=False,
                )
            )
            last_alert = now_unix
        else:
            last_alert = previous.last_alert_unix if previous is not None else 0.0

        state.conditions[key] = ConditionState(
            severity=finding.severity.value,
            consecutive_load_high=previous.consecutive_load_high if previous else 0,
            last_alert_unix=last_alert,
            last_seen_unix=now_unix,
            message=finding.message,
        )

    # Recoveries: previously unhealthy keys for this host that are now clear
    for key, previous in list(state.conditions.items()):
        if not key.startswith(f"{host_name}|"):
            continue
        if key in active_keys:
            continue
        if previous.severity == Severity.OK.value:
            previous.last_seen_unix = now_unix
            continue

        finding_key = key.split("|", 1)[1]
        kind = _kind_from_finding_key(finding_key)
        message = f"{host_name} recovered: {finding_key} back to OK"
        alerts.append(
            AlertEvent(
                host_name=host_name,
                kind=AlertKind.RECOVERY,
                severity=Severity.OK,
                message=message,
                detail=previous.message,
                recovered=True,
            )
        )
        state.conditions[key] = ConditionState(
            severity=Severity.OK.value,
            consecutive_load_high=0 if finding_key == "load" else previous.consecutive_load_high,
            last_alert_unix=now_unix,
            last_seen_unix=now_unix,
            message=message,
        )

    return alerts


def apply_host_unreachable(
    state: MonitorState,
    host_name: str,
    error: str,
    *,
    now_unix: float,
    cooldown_seconds: int,
) -> list[AlertEvent]:
    key = condition_key(host_name, "unreachable")
    previous = state.conditions.get(key)
    should_alert = (
        previous is None
        or previous.severity != Severity.CRITICAL.value
        or now_unix - previous.last_alert_unix >= cooldown_seconds
    )
    message = f"{host_name} unreachable: {error}"
    alerts: list[AlertEvent] = []
    if should_alert:
        alerts.append(
            AlertEvent(
                host_name=host_name,
                kind=AlertKind.HOST_UNREACHABLE,
                severity=Severity.CRITICAL,
                message=message,
                detail=error,
                recovered=False,
            )
        )
        last_alert = now_unix
    else:
        last_alert = previous.last_alert_unix if previous is not None else 0.0

    state.conditions[key] = ConditionState(
        severity=Severity.CRITICAL.value,
        consecutive_load_high=0,
        last_alert_unix=last_alert,
        last_seen_unix=now_unix,
        message=message,
    )
    return alerts


def apply_host_reachable_again(
    state: MonitorState,
    host_name: str,
    *,
    now_unix: float,
) -> list[AlertEvent]:
    key = condition_key(host_name, "unreachable")
    previous = state.conditions.get(key)
    if previous is None or previous.severity == Severity.OK.value:
        return []
    message = f"{host_name} reachable again"
    state.conditions[key] = ConditionState(
        severity=Severity.OK.value,
        consecutive_load_high=0,
        last_alert_unix=now_unix,
        last_seen_unix=now_unix,
        message=message,
    )
    return [
        AlertEvent(
            host_name=host_name,
            kind=AlertKind.RECOVERY,
            severity=Severity.OK,
            message=message,
            detail=previous.message,
            recovered=True,
        )
    ]


def bump_load_streak(state: MonitorState, host_name: str, elevated: bool) -> int:
    key = condition_key(host_name, "load")
    previous = state.conditions.get(key)
    consecutive = (previous.consecutive_load_high if previous else 0) + 1 if elevated else 0
    if previous is None:
        state.conditions[key] = ConditionState(
            severity=Severity.OK.value,
            consecutive_load_high=consecutive,
        )
    else:
        previous.consecutive_load_high = consecutive
    return consecutive


def _kind_from_finding_key(finding_key: str) -> AlertKind:
    if finding_key.startswith("disk:"):
        return AlertKind.DISK
    if finding_key == "ram":
        return AlertKind.RAM
    if finding_key == "load":
        return AlertKind.LOAD
    if finding_key == "unreachable":
        return AlertKind.HOST_UNREACHABLE
    return AlertKind.CHECKER_FAILURE
