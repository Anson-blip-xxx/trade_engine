import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from test_v2_credential_console import case, database, setup
from test_v2_intent_admission import intent

from services.v2_account_admin_http import handler_for
from services.v2_risk_recovery_console import RiskRecoveryConsole
from v2_core.account_risk import AccountScope
from v2_core.capital_model import anchor, health, rehearsal_profile
from v2_core.intents import IntentStore
from v2_core.managed_portfolio import publish_capital_snapshot
from v2_core.runtime_policy import PolicyStore
from v2_core.state import BusinessState, StateKey

__all__ = ["case", "database", "setup"]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def recovery_case(case):
    accounts, _, fixture, _ = case
    connect = accounts.connect
    with connect() as c:
        for name in (
            "20260928_execution_routes.sql",
            "20261010_risk_recovery_console.sql",
        ):
            c.execute((ROOT / "db/migrations" / name).read_text())
        c.execute(
            "INSERT INTO v2_execution_routes VALUES (%s,'SANDBOX',1,%s,1,'ACTIVE',%s,'[]')",
            (accounts.tenant, fixture[4], uuid4()),
        )
    scope = AccountScope("BINANCE", "A", "SANDBOX", "FUTURES")
    settings = (
        PolicyStore(connect, scope)
        .patch(
            {**rehearsal_profile("5000"), "recovery.enabled": True},
            expected_version=0,
            reason="QA",
        )
        .values
    )
    now = time.time_ns() // 1000000
    capital = {
        "base": "977",
        "peak": "1040",
        "drawdown": str(63 / 1040),
        "factor": "0",
        "anchor": anchor(settings),
        "recovery": {"mode": "HALTED", "attempts": 2, "paused_at_ms": now - 8000000},
    }

    def state(namespace, payload):
        key = StateKey(
            **asdict(scope),
            namespace=namespace,
            key="active" if namespace == "runtime-policy-v1" else "latest",
        )
        store = BusinessState(connect)
        old = store.read(key)
        assert (
            store.change(
                key,
                expected_version=old.version if old else 0,
                request_key=str(uuid4()),
                payload=payload,
                reason="QA",
            ).code
            == "APPLIED"
        )
        return key

    payload = {
        "capital_health": capital,
        "account_scope": asdict(scope),
        "started_at_ms": now,
        "valid_until_ms": now + 60000,
        "observation_id": str(uuid4()),
        "account": {
            "totalInitialMargin": "0",
            "availableBalance": "4977",
            "totalWalletBalance": "4977",
            "totalMarginBalance": "4977",
        },
    }
    state("capital-snapshot-v1", payload)
    state("account-coverage-v1", {"status": "ACCOUNT_COVERAGE_CLEAR", "blockers": []})
    state(
        "trading-health-v1",
        {"active": ["CAPITAL_DRAWDOWN_HALT"], "observed_at_ms": now},
    )
    console = RiskRecoveryConsole(connect, tenant_id=accounts.tenant, actor="qa-owner")
    return console, accounts, fixture, scope, settings, payload, state


def action(p, **overrides):
    return {
        "request_id": str(uuid4()),
        "proof": p["proof"],
        "expires_at_ms": p["expires_at_ms"],
        "reason": "QA explicit manual probe",
        **overrides,
    }


def test_real_pg_manual_probe_preserves_capital_and_fails_closed_again(recovery_case):
    con, _, f, scope, settings, before, _ = recovery_case
    p = con.inspect(f[4])
    assert p["eligible"], p
    args = action(p)
    result = con.apply(f[4], **args)
    assert result["status"] == "PROBE_AUTHORIZED" and result["orders_created"] == 0
    assert con.apply(f[4], **args) == result
    after = con.inspect(f[4])["health"]
    for key in ("base", "peak", "drawdown", "anchor"):
        assert after[key] == before["capital_health"][key]
    assert after["recovery"]["attempts"] == 2
    assert after["recovery"]["mode"] == "PROBE" and after["factor"] == "0.20"
    account = {**before["account"], "verified_net_pnl": "-23"}
    # Uses the same deployed engine transition, not a web-only fake recovery.
    healthy_probe = health(account, settings, after, now_ms=result["at_ms"] + 1)
    assert healthy_probe["recovery"]["mode"] == "PROBE"
    failed = health(
        account, settings, after, now_ms=result["at_ms"] + 2, outcomes=("-1", "-1")
    )
    assert failed["recovery"]["mode"] == "HALTED" and failed["factor"] == "0"
    assert (
        health(account, settings, failed, now_ms=result["at_ms"] + 99999999)["factor"]
        == "0"
    )
    publish_capital_snapshot(
        con.connect,
        scope,
        account=before["account"],
        started=result["at_ms"] + 1,
        deadline=result["at_ms"] + 15000,
        observation_id=str(uuid4()),
    )
    assert con.inspect(f[4])["health"]["recovery"]["mode"] == "PROBE"
    with con.connect() as c:
        assert c.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0
        assert (
            c.execute("SELECT count(*) FROM v2_risk_recovery_events").fetchone()[0] == 1
        )
    assert PolicyStore(con.connect, scope).read().values == settings


@pytest.mark.parametrize(
    "change,code",
    [
        ("stale", "CAPITAL_SNAPSHOT_STALE"),
        ("future", "CAPITAL_SNAPSHOT_STALE"),
        ("drawdown", "HARD_DRAWDOWN_LIMIT"),
        ("position", "ACCOUNT_NOT_FLAT"),
        ("coverage", "COVERAGE_NOT_FRESH_CLEAR"),
        ("health", "OTHER_HEALTH_BLOCKERS"),
        ("cooldown", "RECOVERY_COOLDOWN"),
        ("active", "NOT_HALTED"),
        ("attempts", "HARD_HALT_REVIEW_REQUIRED"),
        ("episode", "UNSETTLED_BUSINESS"),
        ("route", "NOT_CURRENT_EXECUTOR"),
        ("policy", "RECOVERY_POLICY_UNSUPPORTED"),
        ("new_policy", "POLICY_SNAPSHOT_NOT_OBSERVED"),
        ("old_health", "OTHER_HEALTH_BLOCKERS"),
    ],
)
def test_unsafe_conditions_rejected_in_database_not_just_ui(
    recovery_case, change, code
):
    con, _, f, _scope, settings, payload, state = recovery_case
    old = con.inspect(f[4])
    if change == "stale":
        payload["valid_until_ms"] = 1
    if change == "future":
        payload["started_at_ms"] += 999999
    if change == "drawdown":
        payload["capital_health"]["base"] = "900"
    if change == "position":
        payload["account"]["totalInitialMargin"] = "1"
    if change == "cooldown":
        payload["capital_health"]["recovery"]["paused_at_ms"] = (
            time.time_ns() // 1000000
        )
    if change == "active":
        payload["capital_health"]["recovery"]["mode"] = "ACTIVE"
    if change == "attempts":
        payload["capital_health"]["recovery"]["attempts"] = 0
    state("capital-snapshot-v1", payload)
    if change == "coverage":
        state(
            "account-coverage-v1",
            {"status": "ACCOUNT_COVERAGE_BLOCKED", "blockers": ["UNKNOWN"]},
        )
    if change == "health":
        state(
            "trading-health-v1",
            {
                "active": ["CAPITAL_DRAWDOWN_HALT", "POSITION_SAFETY_BLOCKED"],
                "observed_at_ms": time.time_ns() // 1000000,
            },
        )
    if change == "episode":
        IntentStore(con.connect).admit(replace(intent(), account_id="A"))
    if change == "route":
        with con.connect() as c:
            c.execute("UPDATE v2_execution_routes SET target_registry=%s", (f[5],))
    if change == "policy":
        state("runtime-policy-v1", {"values": {**settings, "recovery.enabled": False}})
    if change == "new_policy":
        state("runtime-policy-v1", {"values": settings})
    if change == "old_health":
        state(
            "trading-health-v1",
            {"active": ["CAPITAL_DRAWDOWN_HALT"], "observed_at_ms": 1},
        )
    current = con.inspect(f[4])
    assert code in current["blockers"] and not current["eligible"]
    with pytest.raises(ValueError, match="RECOVERY_(NOT_ELIGIBLE|STATE_CHANGED)"):
        con.apply(f[4], **action(old))
    with con.connect() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_risk_recovery_events").fetchone()[0] == 0
        )


def test_stale_proof_expiry_actor_account_and_idempotency(recovery_case):
    con, _, f, _, _, _, _ = recovery_case
    p = con.inspect(f[4])
    with pytest.raises(ValueError, match="PREVIEW_EXPIRED"):
        con.apply(f[4], **action(p, expires_at_ms=1))
    with pytest.raises(ValueError, match="STATE_CHANGED"):
        con.apply(f[4], **action(p, proof={**p["proof"], "epoch": 99}))
    with pytest.raises(ValueError, match="ACCOUNT_NOT_FOUND"):
        con.apply(f[6], **action(p))
    assert con.inspect(f[7])["blockers"] == ["TESTNET_ONLY"]
    with pytest.raises(ValueError, match="NOT_ELIGIBLE"):
        con.apply(f[7], **action(p))
    args = action(p)
    con.apply(f[4], **args)
    with pytest.raises(ValueError, match="REQUEST_CONFLICT"):
        con.apply(f[4], **{**args, "reason": "changed reason"})
    with pytest.raises(ValueError, match="STATE_CHANGED"):
        con.apply(f[4], **action(p))
    assert con.inspect(f[4])["history"][0]["reason"] == args["reason"]


def test_duplicate_concurrent_apply_is_one_authorization(recovery_case):
    con, _, f, *_ = recovery_case
    args = action(con.inspect(f[4]))
    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(lambda _: con.apply(f[4], **args), range(6)))
    assert all(r == results[0] for r in results)
    with con.connect() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_risk_recovery_events").fetchone()[0] == 1
        )


def http(accounts, recovery, registry, body=None, headers=None):
    cls = handler_for(
        accounts,
        authenticated_user="qa-owner",
        origin="https://qa.invalid",
        risk_recovery=recovery,
    )
    h = cls.__new__(cls)
    h.path = f"/api/accounts/{registry}/risk-recovery"
    h.client_address = ("127.0.0.1", 1)
    raw = json.dumps(body).encode() if body is not None else b""
    h.rfile = BytesIO(raw)
    h.headers = {
        "X-V2-Authenticated-User": "qa-owner",
        "Origin": "https://qa.invalid",
        "X-V2-Action": "account-management",
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
        **(headers or {}),
    }
    h._send = lambda status, payload, mime="application/json": (status, payload)
    return h.do_GET() if body is None else h.do_POST()


def test_authenticated_http_to_real_pg_action_and_csrf(recovery_case):
    con, accounts, f, *_ = recovery_case
    status, p = http(accounts, con, f[4])
    assert status == 200 and p["eligible"]
    for headers in (
        {"Origin": "https://evil.invalid"},
        {"X-V2-Authenticated-User": "other"},
        {"X-V2-Action": "strategy-management"},
    ):
        assert http(accounts, con, f[4], action(p), headers)[0] == 403
    assert http(accounts, con, f[4], {**action(p), "tenant_id": f[3]})[0] == 409
    assert http(accounts, con, f[4], action(p))[1]["status"] == "PROBE_AUTHORIZED"
    assert http(accounts, con, f[4])[1]["health"]["recovery"]["mode"] == "PROBE"


def test_narrow_role_cannot_write_state_or_mutate_audit(recovery_case):
    con, _, f, *_ = recovery_case
    role = "qa_recovery_" + uuid4().hex
    with con.connect() as c:
        schema = c.execute("SELECT current_schema()").fetchone()[0]
        c.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        c.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        c.execute(
            sql.SQL("GRANT SELECT ON v2_risk_recovery_events TO {}").format(
                sql.Identifier(role)
            )
        )
        c.execute(
            sql.SQL(
                "GRANT EXECUTE ON FUNCTION v2_web_risk_recovery_check(uuid,uuid),v2_web_risk_recovery_apply(uuid,uuid,uuid,jsonb) TO {}"
            ).format(sql.Identifier(role))
        )

    @contextmanager
    def limited():
        with con.connect() as c:
            c.execute(sql.SQL("SET ROLE {}").format(sql.Identifier(role)))
            yield c

    try:
        limited_con = RiskRecoveryConsole(
            limited, tenant_id=con.tenant, actor="qa-owner"
        )
        p = limited_con.inspect(f[4])
        assert p["eligible"]
        limited_con.apply(f[4], **action(p))
        for query in (
            "UPDATE v2_business_state SET deleted=true",
            "DELETE FROM v2_risk_recovery_events",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), limited() as c:
                c.execute(query)
        with pytest.raises(psycopg.Error), con.connect() as c:
            c.execute("DELETE FROM v2_risk_recovery_events")
        with limited() as c:
            assert not c.execute(
                "SELECT has_function_privilege('public','v2_web_risk_recovery_apply(uuid,uuid,uuid,jsonb)','EXECUTE')"
            ).fetchone()[0]
    finally:
        with con.connect() as c:
            c.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
