"""Workday entry point."""

from __future__ import annotations

from tap_workday.tap import TapWorkday

TapWorkday.cli()
