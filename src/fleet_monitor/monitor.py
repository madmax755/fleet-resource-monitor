"""One check loop over the fleet inventory."""

from __future__ import annotations

import logging
import time

from fleet_monitor.collector import MetricCollectionError, collect_host_metrics
from fleet_monitor.config import AppConfig
from fleet_monitor.models import AlertEvent, Finding, HostCheckResult, HostSpec
from fleet_monitor.ntfy import publish_many
from fleet_monitor.state import (
    apply_findings,
    apply_host_reachable_again,
    apply_host_unreachable,
    load_state,
    save_state,
    write_heartbeat,
)
from fleet_monitor.thresholds import (
    consecutive_required_for,
    evaluate_load,
    evaluate_static_findings,
)

LOGGER = logging.getLogger(__name__)


def check_host(host: HostSpec, config: AppConfig) -> HostCheckResult:
    try:
        metrics = collect_host_metrics(host, config.ssh)
    except MetricCollectionError as exc:
        return HostCheckResult(host_name=host.name, ok=False, error=str(exc))
    except Exception as exc:  # noqa: BLE001 — surface unexpected collector failures
        return HostCheckResult(
            host_name=host.name,
            ok=False,
            error=f"unexpected collector error: {exc}",
        )

    findings = evaluate_static_findings(metrics, config.thresholds)
    return HostCheckResult(host_name=host.name, ok=True, metrics=metrics, findings=findings)


def run_once(config: AppConfig, *, now_unix: float | None = None) -> list[AlertEvent]:
    now = time.time() if now_unix is None else now_unix
    state = load_state(config.state_file)
    all_alerts: list[AlertEvent] = []
    thresholds = config.thresholds

    def required_for(finding_key: str) -> int:
        return consecutive_required_for(finding_key, thresholds)

    for host in config.hosts:
        LOGGER.info("Checking host %s (%s)", host.name, host.host)
        result = check_host(host, config)
        if not result.ok:
            LOGGER.error("Host %s failed: %s", host.name, result.error)
            all_alerts.extend(
                apply_host_unreachable(
                    state,
                    host.name,
                    result.error or "unknown error",
                    now_unix=now,
                    cooldown_seconds=config.alert_cooldown_seconds,
                    consecutive_required=thresholds.unreachable_consecutive_required,
                )
            )
            continue

        assert result.metrics is not None
        LOGGER.info(
            "Host %s OK: ram=%.1f%% load1=%.2f nproc=%d disks=%s",
            host.name,
            result.metrics.ram_used_percent,
            result.metrics.load1,
            result.metrics.nproc,
            ", ".join(f"{d.mount}={d.used_percent:.0f}%" for d in result.metrics.disks),
        )

        all_alerts.extend(
            apply_host_reachable_again(
                state,
                host.name,
                now_unix=now,
                consecutive_required=thresholds.unreachable_consecutive_required,
            )
        )

        findings: list[Finding] = list(result.findings)
        load_finding = evaluate_load(result.metrics, thresholds)
        if load_finding is not None:
            findings.append(load_finding)

        all_alerts.extend(
            apply_findings(
                state,
                host.name,
                findings,
                now_unix=now,
                cooldown_seconds=config.alert_cooldown_seconds,
                consecutive_required=required_for,
            )
        )

    state.last_successful_loop_unix = now
    save_state(config.state_file, state)
    write_heartbeat(config.heartbeat_file, now)

    if all_alerts:
        LOGGER.info("Publishing %d alert(s)", len(all_alerts))
        publish_many(config.ntfy, all_alerts, dry_run=config.dry_run)
    else:
        LOGGER.info("No alert state changes this loop")

    return all_alerts
