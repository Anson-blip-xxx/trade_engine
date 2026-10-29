"""Pure timing tests: no wall-clock sleep, account access or venue calls."""

import pytest
from test_v2_managed_guard_wiring import child_case

from services import v2_managed_testnet as managed
from v2_core.cycle_cadence import cycle_delay

__all__ = ["child_case"]


@pytest.mark.parametrize("degraded", [False, True])
def test_real_managed_child_wires_elapsed_work_and_error_backoff(
    child_case, monkeypatch, degraded
):
    calls = []
    original_create = managed.create_testnet_process

    def create(*args, **kwargs):
        process = original_create(*args, **kwargs)
        original_run = process.daemon.run_once

        def run():
            original_run()  # Stops the fixture after one cycle.
            if degraded:
                raise ConnectionError("qa-no-secret-output")

        process.daemon.run_once = run
        return process

    def delay(interval, **kwargs):
        calls.append((interval, kwargs))
        return cycle_delay(interval, **kwargs)

    monkeypatch.setattr(managed, "create_testnet_process", create)
    monkeypatch.setattr(managed, "cycle_delay", delay)
    child_case.run()
    assert len(calls) == 1
    interval, args = calls[0]
    assert interval == 0  # Nonblocking test fixture, normal config is validated.
    assert args["finished"] >= args["started"]
    assert args["degraded"] is degraded
    assert child_case.sent == ["READY", "DEGRADED" if degraded else "CYCLE"]


@pytest.mark.parametrize(
    "duration,expected", [(0, 15), (6.7, 8.3), (14, 1), (17.2, 1), (100, 1)]
)
def test_work_counts_towards_interval_without_catch_up(duration, expected):
    assert cycle_delay(15, started=100, finished=100 + duration) == pytest.approx(
        expected
    )


@pytest.mark.parametrize("duration", [0, 6.7, 17.2, 100])
def test_failed_cycles_keep_full_error_backoff(duration):
    assert cycle_delay(15, started=100, finished=100 + duration, degraded=True) == 15


def test_repeated_overrun_has_no_burst_or_accumulated_schedule_debt():
    starts, now = [], 0
    for duration in (6, 20, 30, 1):
        starts.append(now)
        finished = now + duration
        now = finished + cycle_delay(15, started=now, finished=finished)
    assert starts == [0, 15, 36, 67]


def test_zero_interval_test_fixture_remains_nonblocking():
    assert cycle_delay(0, started=1, finished=2) == 0


@pytest.mark.parametrize(
    "interval,started,finished,degraded",
    [
        (-1, 0, 1, False),
        (True, 0, 1, False),
        (15, 2, 1, False),
        (15, 0, float("inf"), False),
        (15, float("nan"), 1, False),
        (15, 0, 1, 1),
    ],
)
def test_bad_clock_or_configuration_fails_closed(interval, started, finished, degraded):
    with pytest.raises(ValueError, match="CYCLE_CADENCE_INVALID"):
        cycle_delay(interval, started=started, finished=finished, degraded=degraded)
