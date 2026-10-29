"""Completion-aware cadence without overlapping cycles or catch-up bursts."""

import math


def cycle_delay(interval_seconds, *, started, finished, degraded=False):
    """Count work toward the configured interval; preserve error backoff.

    A slow cycle is never replayed to catch up. The one-second minimum pause
    gives stop/fencing and shared request budgets breathing room on overruns.
    This changes scheduling only, never any venue or risk authorization.
    """
    for value in (interval_seconds, started, finished):
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("CYCLE_CADENCE_INVALID")
    if interval_seconds < 0 or finished < started or type(degraded) is not bool:
        raise ValueError("CYCLE_CADENCE_INVALID")
    if degraded:
        return interval_seconds
    return max(min(1.0, interval_seconds), interval_seconds - (finished - started))
