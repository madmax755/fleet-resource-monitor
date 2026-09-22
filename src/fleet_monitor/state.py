"""Persistent alert state with consecutive confirmation, change detection, and cooldown."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
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

ConsecutiveRequiredFn = Callable[[str], int]


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


def _bump_candidate(
    previous: ConditionState | None,
    observed: str,
) -> tuple[str, int]:
    if previous is None:
        return observed, 1
    if previous.resolved_candidate() == observed:
        return observed, previous.consecutive_count + 1
    return observed, 1


def _confirmed_severity(previous: ConditionState | None) -> Severity:
    if previous is None:
        return Severity.OK
    try:
        return Severity(previous.severity)
    except ValueError:
        return Severity.OK


def _mirror_load_streak(finding_key: str, candidate: str, streak: int) -> int:
    if finding_key == "load" and candidate == Severity.CRITICAL.value:
        return streak
    return 0


def apply_findings(
    state: MonitorState,
    host_name: str,
    findings: list[Finding],
    *,
    now_unix: float,
    cooldown_seconds: int,
    consecutive_required: ConsecutiveRequiredFn | int = 1,
) -> list[AlertEvent]:
    """
    Update state for a reachable host and decide which alerts to emit.

    Alert policy:
    - Require N consecutive observations of a new severity before confirming
      (and alerting) a transition, including recovery to OK. This damps
      ephemeral flaps without slowing the check interval.
    - While still confirmed unhealthy, re-alert at most once per cooldown window
      (default 6h) so critical hosts are not silent forever, but do not spam
      every 15-minute loop.

    ``consecutive_required`` may be an int (same for every key) or a callable
    ``finding_key -> int``. Default 1 preserves legacy immediate transitions.
    """
    required_for: ConsecutiveRequiredFn
    if isinstance(consecutive_required, int):
        required_for = lambda _key, n=consecutive_required: n  # noqa: E731
    else:
        required_for = consecutive_required

    alerts: list[AlertEvent] = []
    active_keys = {condition_key(host_name, finding.key) for finding in findings}

    for finding in findings:
        key = condition_key(host_name, finding.key)
        previous = state.conditions.get(key)
        confirmed = _confirmed_severity(previous)
        candidate, streak = _bump_candidate(previous, finding.severity.value)
        needed = max(1, required_for(finding.key))

        should_alert = False
        new_confirmed = confirmed

        if streak >= needed and finding.severity != confirmed:
            should_alert = True
            new_confirmed = finding.severity
        elif (
            confirmed != Severity.OK
            and finding.severity == confirmed
            and previous is not None
            and now_unix - previous.last_alert_unix >= cooldown_seconds
        ):
            should_alert = True

        if should_alert:
            alerts.append(
                AlertEvent(
                    host_name=host_name,
                    kind=finding.kind,
                    severity=new_confirmed,
                    message=finding.message,
                    detail=finding.detail,
                    recovered=False,
                )
            )
            last_alert = now_unix
        else:
            last_alert = previous.last_alert_unix if previous is not None else 0.0

        state.conditions[key] = ConditionState(
            severity=new_confirmed.value,
            consecutive_load_high=_mirror_load_streak(finding.key, candidate, streak),
            last_alert_unix=last_alert,
            last_seen_unix=now_unix,
            message=finding.message,
            candidate_severity=candidate,
            consecutive_count=streak,
        )

    # Recoveries / OK confirmation: previously tracked keys for this host that
    # are not in the current findings list.
    for key, previous in list(state.conditions.items()):
        if not key.startswith(f"{host_name}|"):
            continue
        if key in active_keys:
            continue

        finding_key = key.split("|", 1)[1]
        if finding_key == "unreachable":
            # Reachability is handled by apply_host_reachable_again.
            continue

        candidate, streak = _bump_candidate(previous, Severity.OK.value)
        needed = max(1, required_for(finding_key))
        confirmed = _confirmed_severity(previous)
        new_confirmed = confirmed
        should_alert = False
        message = previous.message

        if streak >= needed and confirmed != Severity.OK:
            should_alert = True
            new_confirmed = Severity.OK
            message = f"{host_name} recovered: {finding_key} back to OK"
        elif confirmed == Severity.OK:
            message = previous.message or f"{host_name} {finding_key} OK"

        if should_alert:
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
            last_alert = now_unix
        else:
            last_alert = previous.last_alert_unix

        state.conditions[key] = ConditionState(
            severity=new_confirmed.value,
            consecutive_load_high=_mirror_load_streak(finding_key, candidate, streak),
            last_alert_unix=last_alert,
            last_seen_unix=now_unix,
            message=message,
            candidate_severity=candidate,
            consecutive_count=streak,
        )

    return alerts


def apply_host_unreachable(
    state: MonitorState,
    host_name: str,
    error: str,
    *,
    now_unix: float,
    cooldown_seconds: int,
    consecutive_required: int = 1,
) -> list[AlertEvent]:
    key = condition_key(host_name, "unreachable")
    previous = state.conditions.get(key)
    confirmed = _confirmed_severity(previous)
    candidate, streak = _bump_candidate(previous, Severity.CRITICAL.value)
    needed = max(1, consecutive_required)
    message = f"{host_name} unreachable: {error}"

    should_alert = False
    new_confirmed = confirmed
    if streak >= needed and confirmed != Severity.CRITICAL:
        should_alert = True
        new_confirmed = Severity.CRITICAL
    elif (
        confirmed == Severity.CRITICAL
        and previous is not None
        and now_unix - previous.last_alert_unix >= cooldown_seconds
    ):
        should_alert = True

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
        severity=new_confirmed.value,
        consecutive_load_high=0,
        last_alert_unix=last_alert,
        last_seen_unix=now_unix,
        message=message,
        candidate_severity=candidate,
        consecutive_count=streak,
    )
    return alerts


def apply_host_reachable_again(
    state: MonitorState,
    host_name: str,
    *,
    now_unix: float,
    consecutive_required: int = 1,
) -> list[AlertEvent]:
    key = condition_key(host_name, "unreachable")
    previous = state.conditions.get(key)
    if previous is None:
        return []

    confirmed = _confirmed_severity(previous)
    candidate, streak = _bump_candidate(previous, Severity.OK.value)
    needed = max(1, consecutive_required)
    new_confirmed = confirmed
    should_alert = False
    message = previous.message

    if streak >= needed and confirmed != Severity.OK:
        should_alert = True
        new_confirmed = Severity.OK
        message = f"{host_name} reachable again"
    elif confirmed == Severity.OK:
        message = previous.message or f"{host_name} reachable"

    alerts: list[AlertEvent] = []
    if should_alert:
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
        last_alert = now_unix
    else:
        last_alert = previous.last_alert_unix

    state.conditions[key] = ConditionState(
        severity=new_confirmed.value,
        consecutive_load_high=0,
        last_alert_unix=last_alert,
        last_seen_unix=now_unix,
        message=message,
        candidate_severity=candidate,
        consecutive_count=streak,
    )
    return alerts


def bump_load_streak(state: MonitorState, host_name: str, elevated: bool) -> int:
    """Legacy helper kept for tests; prefer consecutive confirmation in apply_findings."""
    key = condition_key(host_name, "load")
    previous = state.conditions.get(key)
    consecutive = (previous.consecutive_load_high if previous else 0) + 1 if elevated else 0
    if previous is None:
        state.conditions[key] = ConditionState(
            severity=Severity.OK.value,
            consecutive_load_high=consecutive,
            candidate_severity=(
                Severity.CRITICAL.value if elevated else Severity.OK.value
            ),
            consecutive_count=consecutive if elevated else 0,
        )
    else:
        previous.consecutive_load_high = consecutive
        if elevated:
            if previous.resolved_candidate() == Severity.CRITICAL.value:
                previous.consecutive_count += 1
            else:
                previous.candidate_severity = Severity.CRITICAL.value
                previous.consecutive_count = 1
        else:
            previous.candidate_severity = Severity.OK.value
            previous.consecutive_count = 1
    return consecutive

