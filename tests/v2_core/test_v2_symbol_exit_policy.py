import pytest

from v2_core.runtime_policy import resolve


def test_symbol_exit_isolation_is_explicitly_opt_in():
    policy = resolve()
    assert policy["exit.symbol_isolation_enabled"] is False
    assert policy["exit.preflight_max_age_ms"] == 10000
    assert resolve({"exit.symbol_isolation_enabled": True})[
        "exit.symbol_isolation_enabled"
    ] is True


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", {}, []])
def test_symbol_isolation_switch_requires_actual_boolean(value):
    with pytest.raises(ValueError):
        resolve({"exit.symbol_isolation_enabled": value})


@pytest.mark.parametrize("value", [None, True, False, 999, 30001, "10000", 10000.0])
def test_reduction_freshness_is_a_bounded_integer(value):
    with pytest.raises(ValueError):
        resolve({"exit.preflight_max_age_ms": value})


@pytest.mark.parametrize("value", [1000, 10000, 30000])
def test_reduction_freshness_accepts_schema_limits(value):
    assert resolve({"exit.preflight_max_age_ms": value})[
        "exit.preflight_max_age_ms"
    ] == value
