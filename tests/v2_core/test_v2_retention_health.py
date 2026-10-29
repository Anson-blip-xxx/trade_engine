import pytest

from services.v2_retention_health import assess, configured_assess


def properties(unit):
    return {
        "LoadState": "loaded",
        "ActiveState": "active" if unit.endswith(".timer") else "inactive",
        "Result": "success",
        "ExecMainExitTimestampMonotonic": str(95000 * 1000000),
    }


def test_current_successful_jobs_are_healthy():
    assert assess(read=properties, now=100000)["active"] == []


@pytest.mark.parametrize(
    "changes,expected",
    [
        ({"LoadState": "not-found"}, "RETENTION_JOB_FAILED"),
        ({"Result": "exit-code"}, "RETENTION_JOB_FAILED"),
        ({"ExecMainExitTimestampMonotonic": "1"}, "RETENTION_JOB_STALE"),
        ({"ExecMainExitTimestampMonotonic": "0"}, "RETENTION_JOB_STALE"),
        (
            {"ExecMainExitTimestampMonotonic": str(101000 * 1000000)},
            "RETENTION_JOB_STALE",
        ),
    ],
)
def test_failure_missing_stale_and_future_completion_are_not_healthy(changes, expected):
    def read(unit):
        return {**properties(unit), **(changes if unit.endswith(".service") else {})}

    assert expected in assess(read=read, now=100000)["active"]


def test_disabled_timer_and_boot_grace():
    def read(unit):
        return {**properties(unit), "ExecMainExitTimestampMonotonic": "0"}

    assert assess(read=read, now=100)["active"] == []

    def disabled(unit):
        return {**read(unit), "ActiveState": "inactive"}

    assert assess(read=disabled, now=100)["active"] == ["RETENTION_TIMER_DISABLED"]


def test_inspection_failure_propagates_instead_of_reporting_recovery():
    def fail(_):
        raise RuntimeError("inspection failed")

    with pytest.raises(RuntimeError):
        assess(read=fail, now=100000)


@pytest.mark.parametrize("enabled,count", [("0", 2), ("1", 3)])
def test_explicit_database_rollout_gate(enabled, count):
    result = configured_assess(
        environment={"V2_RETENTION_DATABASE_ENABLED": enabled},
        read=properties,
        now=100000,
    )
    assert result["active"] == []
    assert len(result["jobs"]) == count


def test_invalid_monitor_configuration_fails_closed():
    with pytest.raises(ValueError, match="CONFIGURATION_INVALID"):
        configured_assess(environment={"V2_RETENTION_DATABASE_ENABLED": "maybe"})
