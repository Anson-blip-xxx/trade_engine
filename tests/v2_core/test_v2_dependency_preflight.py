from services.v2_dependency_preflight import wait_for_dependencies


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
