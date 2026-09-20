"""CLI entrypoint for the fleet resource monitor."""

from __future__ import annotations

import argparse
import logging
import sys
import time
import traceback

from fleet_monitor import __version__
from fleet_monitor.config import AppConfig, load_config
from fleet_monitor.monitor import run_once
from fleet_monitor.ntfy import checker_failure_event, publish_alert
from fleet_monitor.state import load_state, save_state

LOGGER = logging.getLogger(__name__)


def configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fleet-monitor",
        description="SSH fleet resource monitor with ntfy alerts",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run a single check loop then exit",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Log ntfy payloads instead of publishing",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def page_checker_failure(config: AppConfig, message: str, detail: str = "") -> None:
    """Best-effort page when the checker itself blows up."""
    try:
        publish_alert(
            config.ntfy,
            checker_failure_event(message, detail=detail),
            dry_run=config.dry_run,
        )
        state = load_state(config.state_file)
        state.last_checker_alert_unix = time.time()
        save_state(config.state_file, state)
    except Exception:  # noqa: BLE001
        LOGGER.exception("Failed to publish checker-failure alert")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)

    try:
        config = load_config(dry_run=args.dry_run)
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Failed to load configuration")
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    LOGGER.info(
        "fleet-monitor %s starting (hosts=%d interval=%ss once=%s dry_run=%s)",
        __version__,
        len(config.hosts),
        config.check_interval_seconds,
        args.once,
        config.dry_run,
    )

    try:
        if args.once:
            run_once(config)
            return 0

        while True:
            try:
                run_once(config)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Check loop failed")
                detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
                page_checker_failure(
                    config,
                    f"fleet-monitor check loop failed: {detail}",
                    detail=traceback.format_exc(),
                )
            LOGGER.info("Sleeping %s seconds until next check", config.check_interval_seconds)
            time.sleep(config.check_interval_seconds)
    except KeyboardInterrupt:
        LOGGER.info("Interrupted; exiting")
        return 0
    except Exception as exc:  # noqa: BLE001
        LOGGER.exception("Fatal error")
        page_checker_failure(config, f"fleet-monitor fatal: {exc}", detail=traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
