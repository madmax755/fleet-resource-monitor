#!/usr/bin/env python3
"""Console script shim for local runs outside Docker."""

from fleet_monitor.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
