import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from uuid import uuid4

import pytest
from test_v2_runtime_policy import SCOPE, database

from tests.support.v2_admission import evidence, intent
from v2_core.capital_model import rehearsal_profile
from v2_core.entry_controls import read as read_entry_controls
from v2_core.evidence import DecisionEvidence, canonical, digest
from v2_core.liquidity_controls import (
    configure,
    defaults,
    from_snapshot,
    key,
    read,
    resolve,
    validate_submission,
)
from v2_core.runtime_policy import PolicyStore
from v2_core.service import TradingData
from v2_core.state import BusinessState

__all__ = ["database"]


def frozen(*, version=0, changes=None, scope=SCOPE):
    values = resolve(changes or {})
    return {
        "version": version,
        "values": values,
        "digest": digest(
            canonical({"scope": asdict(scope), "version": version, "values": values})
        ),
    }


def enable_guards(database):
    PolicyStore(database, SCOPE).patch(
        rehearsal_profile("1000"), expected_version=0, reason="QA capital baseline"
    )


def prepare(database, saved=None, symbol="BTCUSDT"):
    original = evidence()
    snapshot = json.loads(original.snapshot_json)
    snapshot["symbol"] = symbol
    snapshot["features"] = {
        "context": {} if saved is None else {"liquidity_controls": saved}
    }
    proof = DecisionEvidence(
        original.strategy_version, original.config_json, canonical(snapshot)
    )
    proposal = replace(intent(), symbol=symbol, evidence_ref=proof.evidence_ref)
    data = TradingData(database)
    data.accept(proposal, proof)
    return data.orders.prepare(proposal.intent_id)[0]


@pytest.mark.parametrize(
    "changes",
    [
        None,
        [],
        {"capital.enabled": False},
        {"mode": []},
        {"mode": True},
        {"mode": "enforce"},
        {"mode": "SOFT"},
        {"max_age_ms": True},
        {"max_age_ms": "5000"},
        {"max_age_ms": 5000.0},
        {"max_age_ms": 99},
        {"max_age_ms": 15001},
        {"max_execution_quote_age_ms": True},
        {"max_execution_quote_age_ms": "30000"},
        {"max_execution_quote_age_ms": 30000.0},
        {"max_execution_quote_age_ms": 99},
        {"max_execution_quote_age_ms": 60001},
        {"max_spread_bps": "100.0001"},
        {"max_impact_bps": "100.0001"},
        {"max_price_basis_bps": "500.0001"},
    ],
)
def test_invalid_controls_fail_closed(changes):
    with pytest.raises(ValueError, match="LIQUIDITY_CONTROLS"):
        resolve(changes)


@pytest.mark.parametrize(
    "name", ["max_spread_bps", "max_impact_bps", "max_price_basis_bps"]
)
@pytest.mark.parametrize(
    "value",
    [
        1,
        1.0,
        True,
        None,
        [],
        "NaN",
        "sNaN",
        "Infinity",
        "-Infinity",
        "0",
        "0.0",
        "-0",
        "-0.1",
        " 1",
        "1 ",
        "1\n",
        "+1",
        "1e-3",
        "1_0",
        "01",
        "١",
        "0." + "1" * 31,
    ],
)
def test_bps_require_bounded_positive_plain_decimal_strings(name, value):
    with pytest.raises(ValueError, match="LIQUIDITY_CONTROLS_INVALID_BPS"):
        resolve({name: value})


def test_default_and_boundary_values():
    assert resolve({}) == {
        "mode": "OFF",
        "max_age_ms": 5000,
        "max_execution_quote_age_ms": 30000,
        "max_spread_bps": "20",
        "max_impact_bps": "15",
        "max_price_basis_bps": "100",
    }
    for mode in ("OFF", "SHADOW", "ENFORCE"):
        assert resolve({"mode": mode})["mode"] == mode
    for age in (100, 15000):
        assert resolve({"max_age_ms": age})["max_age_ms"] == age
    for age in (100, 60000):
        assert (
            resolve({"max_execution_quote_age_ms": age})["max_execution_quote_age_ms"]
            == age
        )
    for name, limit in (
        ("max_spread_bps", "100"),
        ("max_impact_bps", "100"),
        ("max_price_basis_bps", "500"),
    ):
        for value in ("0.0001", limit):
            assert resolve({name: value})[name] == value


def test_frozen_snapshot_is_account_bound_and_isolated_from_callers():
    saved = frozen(version=1, changes={"mode": "SHADOW"})
    control = from_snapshot(saved, asdict(SCOPE))
    saved["values"]["mode"] = "OFF"
    copied = control.snapshot()
    copied["values"]["max_age_ms"] = 100
    assert control.values["mode"] == "SHADOW"
    assert control.values["max_age_ms"] == 5000
    with pytest.raises(ValueError, match="LIQUIDITY_CONTROLS_PROOF_INVALID"):
        from_snapshot(control.snapshot(), replace(SCOPE, account_id="other-account"))
    assert from_snapshot(frozen(), SCOPE).values == defaults()
    live = replace(SCOPE, environment="LIVE")
    assert from_snapshot(frozen(scope=live), live).version == 0
    with pytest.raises(ValueError, match="LIQUIDITY_CONTROLS_TESTNET_ONLY"):
        from_snapshot(frozen(version=1, scope=live), live)


@pytest.mark.parametrize(
    "mutation",
    [
        lambda proof: proof.update(extra=True),
        lambda proof: proof.pop("digest"),
        lambda proof: proof.update(version=True),
        lambda proof: proof.update(version=-1),
        lambda proof: proof.update(version=2),
        lambda proof: proof.update(digest="0" * 64),
        lambda proof: proof["values"].pop("max_impact_bps"),
        lambda proof: proof["values"].update(max_impact_bps="16"),
    ],
)
def test_tampered_snapshot_is_rejected(mutation):
    proof = frozen(version=1)
    mutation(proof)
    with pytest.raises(ValueError, match="LIQUIDITY_CONTROLS"):
        from_snapshot(proof, SCOPE)


def test_version_zero_cannot_claim_nondefault_controls_even_with_recomputed_digest():
    with pytest.raises(ValueError, match="LIQUIDITY_CONTROLS_PROOF_INVALID"):
        from_snapshot(frozen(changes={"mode": "ENFORCE"}), SCOPE)


def test_cas_audit_and_account_isolation_preserve_existing_controls(database):
    enable_guards(database)
    policy = PolicyStore(database, SCOPE).read()
    entry = read_entry_controls(database, SCOPE)
    original = read(database, SCOPE)
    assert original.snapshot() == frozen()
    selected = configure(
        database,
        SCOPE,
        {"mode": "SHADOW"},
        expected_version=0,
        reason=" QA liquidity collection ",
        request_key="qa-liquidity-activation",
    )
    assert selected.version == 1 and selected.values["mode"] == "SHADOW"
    assert original.values == defaults() and selected.digest != original.digest
    assert PolicyStore(database, SCOPE).read() == policy
    assert read_entry_controls(database, SCOPE) == entry
    for other in (
        replace(SCOPE, account_id="another-account"),
        replace(SCOPE, environment="LIVE"),
    ):
        assert read(database, other).version == 0
    with pytest.raises(ValueError, match="LIQUIDITY_CONTROLS_VERSION_CONFLICT"):
        configure(
            database, SCOPE, {}, expected_version=0, reason="QA stale configuration"
        )
    with database() as conn:
        row = conn.execute(
            "SELECT request_key,expected_version,version,payload,reason "
            "FROM v2_state_history WHERE state_id=%s",
            (key(SCOPE).identity,),
        ).fetchone()
        assert row == (
            "qa-liquidity-activation",
            0,
            1,
            {"values": selected.values, "reason": "QA liquidity collection"},
            "TESTNET_LIQUIDITY_CONTROLS_CHANGED",
        )
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0


def test_concurrent_configuration_uses_one_cas_winner(database):
    enable_guards(database)

    def write(mode):
        try:
            return configure(
                database, SCOPE, {"mode": mode}, expected_version=0, reason="QA race"
            ).version
        except ValueError as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(write, ("SHADOW", "ENFORCE")))
    assert results.count(1) == 1
    assert results.count("LIQUIDITY_CONTROLS_VERSION_CONFLICT") == 1
    assert read(database, SCOPE).version == 1


def test_live_and_missing_capital_guards_cannot_be_configured(database):
    with pytest.raises(ValueError, match="LIQUIDITY_CONTROLS_TESTNET_ONLY"):
        configure(
            database,
            replace(SCOPE, environment="LIVE"),
            {"mode": "SHADOW"},
            expected_version=0,
            reason="QA disallow live",
        )
    with pytest.raises(ValueError, match="LIQUIDITY_CONTROLS_REQUIRE_CAPITAL_GUARDS"):
        configure(
            database, SCOPE, {"mode": "SHADOW"}, expected_version=0, reason="QA guards"
        )
    assert read(database, SCOPE).version == 0


@pytest.mark.parametrize(
    "arguments",
    [
        {"expected_version": True},
        {"expected_version": -1},
        {"reason": ""},
        {"reason": "abcd"},
        {"reason": "a" * 241},
        {"reason": "QA\nreason"},
        {"request_key": ""},
        {"request_key": " padded"},
        {"request_key": "a" * 241},
        {"request_key": "qa\nrequest"},
        {"request_key": 1},
    ],
)
def test_invalid_write_metadata_rejected_before_database_access(arguments):
    def no_connection():
        pytest.fail("invalid configuration must not access the database")

    with pytest.raises(ValueError, match="LIQUIDITY_CONTROLS"):
        configure(
            no_connection,
            SCOPE,
            {},
            **{"expected_version": 0, "reason": "QA metadata", **arguments},
        )


def test_submission_accepts_old_absent_proof_only_until_first_configuration(database):
    order = prepare(database)
    with database() as conn:
        validate_submission(conn, SCOPE, order)
        with pytest.raises(ValueError, match="LIQUIDITY_CONTROLS_ORDER_MISSING"):
            validate_submission(conn, SCOPE, str(uuid4()))
        with pytest.raises(ValueError, match="LIQUIDITY_CONTROLS_ORDER_MISSING"):
            validate_submission(conn, replace(SCOPE, account_id="other-account"), order)
    enable_guards(database)
    configure(database, SCOPE, {}, expected_version=0, reason="QA explicit OFF")
    with (
        database() as conn,
        pytest.raises(ValueError, match="LIQUIDITY_CONTROLS_CHANGED_REPROPOSE"),
    ):
        validate_submission(conn, SCOPE, order)


def test_submission_rejects_changed_or_tampered_frozen_controls(database):
    enable_guards(database)
    selected = configure(
        database, SCOPE, {"mode": "SHADOW"}, expected_version=0, reason="QA collection"
    )
    order = prepare(database, selected.snapshot())
    tampered = deepcopy(selected.snapshot())
    tampered["values"]["max_age_ms"] = 100
    bad_order = prepare(database, tampered, symbol="ETHUSDT")
    with database() as conn:
        validate_submission(conn, SCOPE, order)
        with pytest.raises(ValueError, match="LIQUIDITY_CONTROLS_CHANGED_REPROPOSE"):
            validate_submission(conn, SCOPE, bad_order)
    configure(
        database, SCOPE, {"mode": "OFF"}, expected_version=1, reason="QA disable gate"
    )
    with (
        database() as conn,
        pytest.raises(ValueError, match="LIQUIDITY_CONTROLS_CHANGED_REPROPOSE"),
    ):
        validate_submission(conn, SCOPE, order)


def test_deleted_controls_cannot_silently_fall_back_to_off(database):
    enable_guards(database)
    configure(database, SCOPE, {}, expected_version=0, reason="QA audited OFF")
    result = BusinessState(database).change(
        key(SCOPE),
        expected_version=1,
        request_key="qa-tombstone",
        payload={},
        reason="QA deleted controls",
        deleted=True,
    )
    assert result.code == "APPLIED"
    with pytest.raises(ValueError, match="LIQUIDITY_CONTROLS_DELETED"):
        read(database, SCOPE)
