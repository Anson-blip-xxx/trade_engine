from decimal import Decimal

from test_v2_capital_model import account
from test_v2_runtime_policy import SCOPE
from test_v2_runtime_policy import database as database_fixture

from v2_core.capital_model import health, rehearsal_profile
from v2_core.runtime_policy import resolve

database = database_fixture


def settings(**changes):
    return resolve({**rehearsal_profile("10000"), "recovery.enabled": True, **changes})


def test_pause_probe_and_restart_do_not_reset_attempts():
    policy = settings()
    paused = health(account("9940"), policy, now_ms=1000)
    assert paused["factor"] == "0"
    assert paused["recovery"]["mode"] == "PAUSED"
    assert health(account("9940"), policy, paused)["recovery"] == paused["recovery"]
    probe = health(account("9940"), policy, paused, now_ms=7201000)
    assert probe["recovery"]["mode"] == "PROBE"
    assert Decimal(probe["factor"]) == Decimal(".2")
    failed = health(account("9935"), policy, probe, now_ms=7202000)
    assert failed["recovery"]["mode"] == "PAUSED"
    second = health(account("9935"), policy, failed, now_ms=14402000)
    assert second["recovery"]["attempts"] == 2
    stopped = health(account("9930"), policy, second, now_ms=14403000)
    assert stopped["recovery"]["mode"] == "HALTED"
    # Neither later clocks nor recovered wallet nor disabling recovery unlocks.
    for p in (policy, {**policy, "recovery.enabled": False}):
        assert health(account("10000"), p, stopped, now_ms=999999999)["factor"] == "0"


def test_hard_halt_latches_across_midnight_and_recovered_floating_loss():
    policy = settings()
    stopped = health(account("9900"), policy, now_ms=86399000)
    assert stopped["recovery"]["mode"] == "HALTED"
    assert health(account(), policy, stopped, now_ms=86401000)["factor"] == "0"


def test_restore_requires_profit_sample_and_drawdown_recovery():
    policy = settings()
    paused = health(account("9940"), policy, now_ms=0)
    probe = health(account("9940"), policy, paused, now_ms=7200000)
    assert (
        health(account("9980"), policy, probe, outcomes=("1",))["recovery"]["mode"]
        == "PROBE"
    )
    assert (
        health(account("9945"), policy, probe, outcomes=("1", "1", "1"))["recovery"][
            "mode"
        ]
        == "PROBE"
    )
    recovered = health(account("9980"), policy, probe, outcomes=("1", "1", "1"))
    assert recovered["recovery"]["mode"] == "ACTIVE"
    assert recovered["recovery"]["attempts"] == 1


def test_two_probe_losses_pause_even_if_floating_profit_masks_loss():
    policy = settings()
    paused = health(account("9940"), policy, now_ms=0)
    probe = health(account("9940"), policy, paused, now_ms=7200000)
    result = health(account("9950"), policy, probe, outcomes=("-1", "-1"))
    assert result["factor"] == "0"


def test_2000_capital_threshold_is_proportional_not_fixed_100():
    policy = settings(**{"capital.initial_equity": "2000"})
    assert health(account("9900"), policy)["recovery"]["mode"] == "ACTIVE"
    assert health(account("9800"), policy)["recovery"]["mode"] == "HALTED"


def test_backwards_clock_cannot_unlock_pause():
    policy = settings()
    paused = health(account("9940"), policy, now_ms=8000000)
    assert health(account("9940"), policy, paused, now_ms=0)["factor"] == "0"


def test_postgres_pause_probe_and_halt_survive_reconstruction(database):
    from v2_core.capital_model import current_health
    from v2_core.managed_portfolio import publish_capital_snapshot
    from v2_core.runtime_policy import PolicyStore

    profile = PolicyStore(database, SCOPE).patch(
        settings(), expected_version=0, reason="QA recovery"
    )

    def observe(wallet, clock):
        publish_capital_snapshot(
            database,
            SCOPE,
            account=account(wallet),
            started=clock,
            deadline=clock + 1000,
            observation_id=str(clock),
        )
        return current_health(database, SCOPE, account(wallet), profile.values)

    assert observe("9940", 1000)["recovery"]["mode"] == "PAUSED"
    assert observe("9940", 7201000)["recovery"]["mode"] == "PROBE"
    assert observe("9900", 7202000)["recovery"]["mode"] == "HALTED"
    assert observe("10000", 90000000)["factor"] == "0"
