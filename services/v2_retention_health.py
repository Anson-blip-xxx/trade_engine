"""Read-only scheduler checks; never starts jobs or loads trading credentials."""

import os
import subprocess
import time

JOBS = {
    "trade-v2-qa-gc": 3 * 3600,
    "trade-v2-diagnostic-retention": 3 * 3600,
    "trade-v2-legacy-log-gc": 25 * 3600,
}


def properties(unit):
    result = subprocess.run(
        [
            "systemctl",
            "show",
            unit,
            "-p",
            "LoadState",
            "-p",
            "ActiveState",
            "-p",
            "Result",
            "-p",
            "ExecMainExitTimestampMonotonic",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
    )
    return dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )


def assess(*, read=properties, now=None, include_database=True):
    now = time.monotonic() if now is None else now
    active = set()
    jobs = {}
    for name, limit in JOBS.items():
        if name == "trade-v2-diagnostic-retention" and not include_database:
            continue
        timer = read(name + ".timer")
        service = read(name + ".service")
        if timer.get("LoadState") != "loaded" or timer.get("ActiveState") != "active":
            active.add("RETENTION_TIMER_DISABLED")
        if service.get("LoadState") != "loaded" or service.get("Result") != "success":
            active.add("RETENTION_JOB_FAILED")
        ended = int(service.get("ExecMainExitTimestampMonotonic", "0")) / 1000000
        age = now - ended if ended else None
        if (age is None and now > 900) or (age is not None and not 0 <= age <= limit):
            active.add("RETENTION_JOB_STALE")
        jobs[name] = {
            "result": service.get("Result", "unknown"),
            "age_seconds": int(age) if age is not None else None,
        }
    return {"active": sorted(active), "jobs": jobs}


def configured_assess(*, environment=None, **kwargs):
    environment = os.environ if environment is None else environment
    enabled = environment.get("V2_RETENTION_DATABASE_ENABLED", "0")
    if enabled not in {"0", "1"}:
        raise ValueError("RETENTION_MONITOR_CONFIGURATION_INVALID")
    return assess(include_database=enabled == "1", **kwargs)
