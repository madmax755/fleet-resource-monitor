"""ntfy.sh notification client."""

from __future__ import annotations

import logging
import urllib.error
import urllib.request

from fleet_monitor.config import NtfyConfig
from fleet_monitor.models import AlertEvent, AlertKind, Severity

LOGGER = logging.getLogger(__name__)


def priority_for(severity: Severity) -> str:
    if severity is Severity.CRITICAL:
        return "high"
    if severity is Severity.WARN:
        return "default"
    return "low"


def tags_for(event: AlertEvent) -> str:
    if event.recovered or event.severity is Severity.OK:
        return "white_check_mark,recovery"
    if event.severity is Severity.CRITICAL:
        return "rotating_light,critical"
    if event.severity is Severity.WARN:
        return "warning,warn"
    return "information_source"


def publish_alert(
    config: NtfyConfig,
    event: AlertEvent,
    *,
    dry_run: bool = False,
) -> None:
    url = f"{config.url.rstrip('/')}/{config.topic}"
    title = event.message
    body_lines = [event.message]
    if event.detail:
        body_lines.append(event.detail)
    body_lines.append(f"host={event.host_name} kind={event.kind.value}")
    body = "\n".join(body_lines)

    headers: dict[str, str] = {
        "Title": title[:250],
        "Priority": priority_for(event.severity),
        "Tags": tags_for(event),
        "Content-Type": "text/plain; charset=utf-8",
    }
    if config.token:
        headers["Authorization"] = f"Bearer {config.token}"

    if dry_run:
        LOGGER.info(
            "[dry-run] ntfy %s priority=%s title=%r body=%r",
            url,
            headers["Priority"],
            title,
            body,
        )
        return

    request = urllib.request.Request(
        url,
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            LOGGER.info(
                "ntfy published (%s) for %s: %s",
                response.status,
                event.host_name,
                event.message,
            )
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ntfy HTTP {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"ntfy request failed: {exc}") from exc


def publish_many(
    config: NtfyConfig,
    events: list[AlertEvent],
    *,
    dry_run: bool = False,
) -> None:
    for event in events:
        publish_alert(config, event, dry_run=dry_run)


def checker_failure_event(message: str, detail: str = "") -> AlertEvent:
    return AlertEvent(
        host_name="fleet-monitor",
        kind=AlertKind.CHECKER_FAILURE,
        severity=Severity.CRITICAL,
        message=message,
        detail=detail
        or "The fleet resource monitor itself failed; investigate the container logs.",
        recovered=False,
    )
