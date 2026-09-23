import importlib.metadata
from pathlib import Path

from services.v2_dependency_preflight import (
    _RUNTIME_REQUIREMENTS,
    runtime_ready,
    wait_for_dependencies,
)


def test_runtime_gate_matches_complete_lock_file():
    lines = Path("requirements-v2-runtime-lock.txt").read_text().splitlines()
    locked = dict(
        line.split("==", 1) for line in lines if line and not line.startswith("#")
    )
    assert locked == _RUNTIME_REQUIREMENTS


def test_runtime_requires_exact_tested_python_and_driver_versions():
    expected = {
        "backports.zstd": "1.7.0",
        "certifi": "2026.7.22",
        "clickhouse-connect": "1.6.0",
        "lz4": "4.4.5",
        "psycopg": "3.3.6",
        "psycopg-binary": "3.3.6",
        "redis": "8.0.1",
        "typing_extensions": "4.16.0",
        "urllib3": "2.8.0",
    }
    assert runtime_ready(version=expected.__getitem__, python_version=(3, 12, 3))
    wrong = {**expected, "psycopg": "3.1.17"}
    assert not runtime_ready(version=wrong.__getitem__, python_version=(3, 12, 3))
    assert not runtime_ready(version=expected.__getitem__, python_version=(3, 13, 0))


def test_runtime_fails_closed_when_a_driver_is_missing():
    def missing(_):
        raise importlib.metadata.PackageNotFoundError

    assert not runtime_ready(version=missing, python_version=(3, 12, 3))


def test_all_dependencies_ready_without_waiting():
    calls = []
    assert wait_for_dependencies(
        (lambda: calls.append("pg") or True, lambda: calls.append("redis") or True),
        monotonic=lambda: 0,
        sleep=lambda _: calls.append("sleep"),
    )
    assert calls == ["pg", "redis"]


def test_transient_failure_retries_without_leaking_exception_text():
    ticks = iter((0, 0, 0.5))
    attempts, waits = [], []

    def probe():
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("secret-endpoint")
        return True

    assert wait_for_dependencies(
        (probe,), monotonic=lambda: next(ticks), sleep=waits.append
    )
    assert len(attempts) == 2 and waits == [0.5]


def test_timeout_and_clock_rollback_fail_closed():
    ticks = iter((0, 30))
    assert not wait_for_dependencies(
        (lambda: False,), monotonic=lambda: next(ticks), sleep=lambda _: None
    )
    ticks = iter((10, 9))
    assert not wait_for_dependencies(
        (lambda: False,), monotonic=lambda: next(ticks), sleep=lambda _: None
    )
