"""Tests for metric output parsing and inventory loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from fleet_monitor.collector import MetricCollectionError, parse_metrics_output
from fleet_monitor.config import load_hosts_inventory


SAMPLE_OUTPUT = """\
DISK\t/\t71
DISK\t/mnt/data\t88
MEM\t2048000\t8192000
LOAD\t1.25
NPROC\t4
"""


def test_parse_metrics_output() -> None:
    metrics = parse_metrics_output("box", SAMPLE_OUTPUT)
    assert metrics.host_name == "box"
    assert len(metrics.disks) == 2
    assert metrics.disks[0].mount == "/"
    assert metrics.disks[0].used_percent == 71.0
    assert metrics.ram_used_percent == pytest.approx(75.0)
    assert metrics.load1 == 1.25
    assert metrics.nproc == 4


def test_parse_metrics_rejects_incomplete() -> None:
    with pytest.raises(MetricCollectionError):
        parse_metrics_output("box", "LOAD\t1.0\n")


def test_load_hosts_resolves_proxy_jump_name(tmp_path: Path) -> None:
    inventory = tmp_path / "hosts.yml"
    inventory.write_text(
        """
hosts:
  - name: server1
    host: 100.100.126.125
    user: max-kendall
  - name: proxmox
    host: 100.78.145.98
    user: root
    proxy_jump: server1
  - name: pi
    host: localhost
    user: local
    local: true
""",
        encoding="utf-8",
    )
    hosts = {host.name: host for host in load_hosts_inventory(inventory)}
    assert hosts["proxmox"].proxy_jump == "max-kendall@100.100.126.125"
    assert hosts["pi"].local is True
