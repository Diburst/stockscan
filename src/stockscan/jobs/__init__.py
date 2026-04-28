"""Scheduled job orchestration. Each job is a single CLI entry point that
runs the same logic the scheduler will invoke at its scheduled time.

Phase 3 ships the nightly-scan job. Phase 4 will add place-orders + reconcile.
"""

from stockscan.jobs.nightly import NightlyResult, run_nightly_scan

__all__ = ["NightlyResult", "run_nightly_scan"]
