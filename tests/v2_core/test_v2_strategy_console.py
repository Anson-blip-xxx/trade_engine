import json
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from test_v2_credential_console import case, database, setup
from test_v2_trading_pipeline import case as pipeline_case

from services.v2_account_admin_http import handler_for
from services.v2_strategy_console import StrategyConsole, preflight
from v2_core.account_risk import AccountScope
from v2_core.capital_model import rehearsal_profile
from v2_core.runtime_policy import PolicyStore
from v2_core.state import BusinessState

__all__ = ["case", "database", "pipeline_case", "setup"]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def console_case(case):
    accounts, _, setup, _ = case
    with accounts.connect() as c:
        c.execute((ROOT / "db/migrations/20260928_strategy_console.sql").read_text())
        schema = c.execute("SELECT current_schema()").fetchone()[0]
        index_sql = (
            ROOT / "deploy/v2-testnet/strategy-console-indexes.sql"
        ).read_text()
        # QA schemas are isolated and have no concurrent writers at migration time.
        c.execute(
            index_sql.replace("CONCURRENTLY ", "").replace(
                "trade_v2.", sql.Identifier(schema).as_string(c) + "."
            )
        )
    for account in ("A", "B"):
        PolicyStore(
            accounts.connect, AccountScope("BINANCE", account, "SANDBOX", "FUTURES")
        ).patch(rehearsal_profile("1000"), expected_version=0, reason="QA initial")
    console = StrategyConsole(
        accounts.connect, tenant_id=accounts.tenant, actor="qa-owner"
    )
    return console, accounts, setup


def preview(console, registry, **overrides):
    args = {"expected_version": 1, "changes": {"sizing.max_margin_fraction": "0.10"}}
    args.update(overrides)
    return console.preview(registry, **args)


def action(preview, **overrides):
    result = {
        "expected_version": preview["version"],
        "changes": preview["changes"],
        "restore_version": preview["restore_version"],
        "preview_digest": preview["preview_digest"],
        "preview_expires_at_ms": preview["preview_expires_at_ms"],
        "reason": "QA reviewed configuration",
        "request_id": str(uuid4()),
    }
    return {**result, **overrides}


def test_preview_is_read_only_and_publish_changes_only_selected_account(console_case):
    console, _, setup = console_case
    registry, other = setup[4:6]
    p = preview(console, registry)
    assert (
        not p["execution_authorized"]
        and p["before"]["kind"] == "ILLUSTRATION_NOT_BACKTEST"
    )
    assert console.inspect(registry)["version"] == 1
    args = action(p)
    first = console.apply(registry, **args)
    assert first["version"] == 2
    assert console.apply(registry, **args) == first
    assert console.inspect(other)["values"]["sizing.max_margin_fraction"] == "0.05"
    current = console.inspect(registry)
    assert (
        current["version"] == 2
        and current["values"]["sizing.max_margin_fraction"] == "0.10"
    )
    with console.connect() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_policy_console_events").fetchone()[0]
            == 1
        )
        assert c.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0
    with pytest.raises(ValueError, match="REQUEST_CONFLICT"):
        console.apply(registry, **{**args, "reason": "different"})


def test_concurrent_publish_has_single_version_and_retry_is_idempotent(console_case):
    console, _, setup = console_case
    p = preview(console, setup[4])
    args = action(p)

    def attempt(_):
        try:
            return console.apply(setup[4], **args)
        except ValueError as e:
            assert str(e) == "POLICY_VERSION_CONFLICT"
            return console.apply(setup[4], **args)

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(attempt, range(8)))
    assert all(r == results[0] for r in results)
    assert results[0]["version"] == 2


def test_stale_preview_and_tampering_never_overwrite_newer_configuration(console_case):
    console, _, setup = console_case
    p = preview(console, setup[4])
    with pytest.raises(ValueError, match="PREVIEW_MISMATCH"):
        console.apply(
            setup[4], **action(p, changes={"sizing.max_margin_fraction": "0.15"})
        )
    with pytest.raises(ValueError, match="PREVIEW_MISMATCH"):
        console.apply(setup[5], **action(p))
    console.apply(setup[4], **action(p))
    with pytest.raises(ValueError, match="VERSION_CONFLICT"):
        console.apply(setup[4], **action(p))


def test_restore_creates_new_version_but_never_restores_execution_flags(console_case):
    console, _, setup = console_case
    registry = setup[4]
    console.apply(registry, **action(preview(console, registry)))
    scope = AccountScope("BINANCE", "A", "SANDBOX", "FUTURES")
    # Trusted operator enables a separate capability; web restoration cannot undo it.
    PolicyStore(console.connect, scope).patch(
        {"quant.admission_enabled": True}, expected_version=2, reason="QA operator"
    )
    p = preview(console, registry, expected_version=3, changes={}, restore_version=1)
    assert p["changes"] == {"sizing.max_margin_fraction": "0.05"}
    result = console.apply(registry, **action(p))
    assert result["version"] == 4
    assert console.inspect(registry)["values"]["quant.admission_enabled"] is True
    with pytest.raises(psycopg.Error), console.connect() as c:
        c.execute("UPDATE v2_policy_console_events SET request='{}'")


def test_patch_preserves_legacy_payload_without_inserting_new_default_keys(
    console_case,
):
    console, _, setup = console_case
    store = PolicyStore(
        console.connect, AccountScope("BINANCE", "A", "SANDBOX", "FUTURES")
    )
    values = {
        k: v for k, v in store.read().values.items() if not k.startswith("quant.")
    }
    BusinessState(console.connect).change(
        store.key,
        expected_version=1,
        request_key="qa-legacy",
        payload={"values": values},
        reason="QA_LEGACY",
    )
    p = preview(console, setup[4], expected_version=2)
    console.apply(setup[4], **action(p))
    raw = json.loads(BusinessState(console.connect).read(store.key).payload_json)[
        "values"
    ]
    assert not any(k.startswith("quant.") for k in raw)
    assert raw["sizing.max_margin_fraction"] == "0.10"


def test_foreign_live_retired_and_unprovisioned_accounts_cannot_be_modified(
    console_case,
):
    console, _, setup = console_case
    for registry, expected in [(setup[6], "NOT_FOUND"), (setup[7], "SANDBOX_ONLY")]:
        with pytest.raises(ValueError, match=expected):
            preview(console, registry)
    assert console.inspect(setup[7])["editable"] is False
    with console.connect() as c:
        c.execute(
            "INSERT INTO v2_account_retirements(tenant_id,registry_id,reason) VALUES (%s,%s,'USER_REMOVED_INVALID_CREDENTIAL')",
            (console.tenant, setup[5]),
        )
    with pytest.raises(ValueError, match="NOT_FOUND"):
        console.inspect(setup[5])
    with pytest.raises(ValueError, match="PROVISIONED"):
        preview(console, setup[4], expected_version=0)


def test_account_submit_lock_blocks_policy_change_and_atomic_event_failure_rolls_back(
    console_case,
):
    console, _, setup = console_case
    registry = setup[4]
    p = preview(console, registry)
    scope = AccountScope("BINANCE", "A", "SANDBOX", "FUTURES")
    with console.connect() as c:
        c.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("testnet-protocol-account:" + scope.key,),
        )
        with pytest.raises(psycopg.errors.LockNotAvailable):
            console.apply(registry, **action(p))
    assert console.inspect(registry)["version"] == 1
    with console.connect() as c:
        c.execute(
            "ALTER TABLE v2_policy_console_events ADD CONSTRAINT qa_failure CHECK(expected_version<0)"
        )
    with pytest.raises(psycopg.Error):
        console.apply(registry, **action(p))
    assert console.inspect(registry)["version"] == 1


@pytest.fixture
def restricted(console_case):
    console, _, setup = console_case
    role = "qa_policy_web_" + uuid4().hex
    with console.connect() as c:
        schema = c.execute("SELECT current_schema()").fetchone()[0]
        c.execute(
            sql.SQL("CREATE ROLE {} NOSUPERUSER NOINHERIT").format(sql.Identifier(role))
        )
        c.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        # Only SELECT privileges already present in account-admin-grants.sql.
        for table in (
            "v2_tenant_accounts",
            "v2_account_retirements",
            "v2_trade_intents",
            "v2_settlements",
        ):
            c.execute(
                sql.SQL("GRANT SELECT ON {} TO {}").format(
                    sql.Identifier(table), sql.Identifier(role)
                )
            )
        grants = (ROOT / "deploy/v2-testnet/strategy-console-grants.sql").read_text()
        grants = grants.replace(
            "trade_v2.", sql.Identifier(schema).as_string(c) + "."
        ).replace("tradev2accounts", sql.Identifier(role).as_string(c))
        c.execute(grants)

    @contextmanager
    def web_connect():
        with console.connect() as c:
            c.execute(
                sql.SQL("SET SESSION AUTHORIZATION {}").format(sql.Identifier(role))
            )
            yield c

    try:
        yield (
            StrategyConsole(web_connect, tenant_id=console.tenant, actor="qa-owner"),
            setup,
        )
    finally:
        with console.connect() as c:
            c.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            c.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def test_real_deployment_acl_reads_previews_applies_and_denies_direct_state_writes(
    restricted,
):
    console, setup = restricted
    preflight(console.connect)
    p = preview(console, setup[4])
    assert console.apply(setup[4], **action(p))["version"] == 2
    assert console.inspect(setup[4])["version"] == 2
    assert console.analytics(setup[4])["summary"]["settled_trades"] == 0
    for statement in (
        "UPDATE v2_business_state SET payload='{}'",
        "DELETE FROM v2_state_history",
        "UPDATE v2_orders SET status='CANCELLED'",
    ):
        with (
            pytest.raises(psycopg.errors.InsufficientPrivilege),
            console.connect() as c,
        ):
            c.execute(statement)


def test_actual_web_role_reads_scoped_equity_and_cash_facts(restricted, console_case):
    from test_v2_equity_analytics import income, snapshot

    web, setup = restricted
    owner = console_case[0]
    scope = AccountScope("BINANCE", "A", "SANDBOX", "FUTURES")
    now = time.time_ns() // 1000000
    snapshot(owner.connect, now - 100, scope=scope)
    snapshot(owner.connect, now - 10, scope=scope, wallet="1100", equity="1090")
    income(owner.connect, now - 50, "100", scope=scope)
    report = web.analytics(setup[4])["equity"]
    assert report["samples"] == 2 and report["cash"]["events"] == 1
    assert report["latest"]["unrealized"] == "-10"
    assert web.analytics(setup[5])["equity"]["samples"] == 0


def test_missing_read_index_fails_preflight_before_opening_http(console_case):
    console, _, _ = console_case
    preflight(console.connect)
    with console.connect() as c:
        c.execute("DROP INDEX v2_decisions_analysis_recent")
    with pytest.raises(ValueError, match="READ_INDEXES_REQUIRED"):
        preflight(console.connect)


@pytest.mark.parametrize(
    "changes",
    [
        {"capital.enabled": False},
        {"quant.admission_enabled": True},
        {"capital.pool_fraction": "0.8", "sizing.pool_fraction": "0.8"},
        {"sizing.pool_fraction": "0.3"},
        {"sizing.max_margin_fraction": "NaN"},
        {"sizing.risk_fraction": "0.1"},
        {"sizing.max_margin_fraction": 0.1},
    ],
)
def test_database_function_rejects_bypass_of_web_validation(restricted, changes):
    console, setup = restricted
    document = {
        "changes": changes,
        "actor": "qa",
        "reason": "QA direct invocation",
        "preview_digest": "a" * 64,
        "preview_expires_at_ms": time.time_ns() // 1000000 + 300000,
        "restore_version": None,
    }
    with pytest.raises(psycopg.Error), console.connect() as c:
        c.execute(
            "SELECT v2_web_policy_patch(%s,%s,1,%s,%s)",
            (console.tenant, setup[4], uuid4(), Jsonb(document)),
        )
    assert console.inspect(setup[4])["version"] == 1


def http(accounts, strategy, path, body=None, *, headers=None, peer="127.0.0.1"):
    cls = handler_for(
        accounts,
        authenticated_user="qa-owner",
        origin="https://console.invalid",
        strategy_console=strategy,
    )
    h = cls.__new__(cls)
    h.path, h.client_address = path, (peer, 1234)
    raw = json.dumps(body).encode() if body is not None else b""
    h.rfile = BytesIO(raw)
    h.headers = {
        "X-V2-Authenticated-User": "qa-owner",
        "Origin": "https://console.invalid",
        "X-V2-Action": "strategy-management",
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
        **(headers or {}),
    }
    h._send = lambda status, payload, mime="application/json": (status, payload)
    return h.do_POST() if body is not None else h.do_GET()


def test_http_preview_apply_read_and_csrf_scope_controls(console_case):
    console, accounts, setup = console_case
    path = f"/api/accounts/{setup[4]}/strategy/"
    assert http(accounts, console, "/accounts/strategy")[0] == 200
    assert http(accounts, None, "/accounts/strategy")[0] == 404
    status, p = http(
        accounts,
        console,
        path + "preview",
        {
            "expected_version": 1,
            "changes": {"sizing.max_margin_fraction": "0.10"},
            "restore_version": None,
        },
    )
    assert status == 200
    args = action(p)
    for headers in (
        {"Origin": "https://evil.invalid"},
        {"X-V2-Authenticated-User": "other"},
        {"X-V2-Action": "account-management"},
    ):
        assert http(accounts, console, path + "apply", args, headers=headers)[0] == 403
    assert http(accounts, console, path + "apply", args, peer="10.0.0.1")[0] == 403
    assert http(accounts, console, path + "apply", args)[1]["version"] == 2
    assert http(accounts, console, path + "policy")[1]["version"] == 2
    assert http(accounts, console, path + "analytics?days=14")[0] == 200
    assert (
        http(accounts, console, f"/api/accounts/{setup[6]}/strategy/policy")[0] == 404
    )


def test_shadow_http_auth_scope_and_readonly_no_install(console_case):
    console, accounts, setup = console_case
    path = f"/api/accounts/{setup[4]}/strategy/shadow"
    assert http(accounts, console, path)[1]["health"]["status"] == "NOT_INSTALLED"
    assert (
        http(accounts, console, path, headers={"X-V2-Authenticated-User": "wrong"})[0]
        == 403
    )
    assert (
        http(accounts, console, f"/api/accounts/{setup[6]}/strategy/shadow")[0] == 404
    )
    # GET validation uses the existing uniform not-found response (no scope leaks).
    assert http(accounts, console, path + "?days=7&days=14")[0] == 404
    assert http(accounts, console, path + "?days=2")[0] == 404
    assert http(accounts, console, path, {})[0] == 400
    with console.connect() as conn:
        conn.execute((ROOT / "db/migrations/20261001_reentry_observer.sql").read_text())
    assert http(accounts, console, path)[1]["rows"] == []
    assert console.inspect(setup[4])["version"] == 1


def test_shadow_busy_auth_and_failure_release(console_case, monkeypatch):
    console, _, setup = console_case
    console._shadow_slot.acquire()
    try:
        assert console.shadow(setup[4])["status"] == "BUSY"
        with pytest.raises(ValueError, match="ACCOUNT_NOT_FOUND"):
            console.shadow(setup[6])
    finally:
        console._shadow_slot.release()
    import services.v2_reentry_observation_report as module

    original = module.observation_report

    def fail(*args, **kwargs):
        raise TimeoutError("qa")

    monkeypatch.setattr(module, "observation_report", fail)
    with pytest.raises(TimeoutError):
        console.shadow(setup[4])
    monkeypatch.setattr(module, "observation_report", original)
    assert console.shadow(setup[4])["health"]["status"] == "NOT_INSTALLED"


def test_preview_expiry_rejects_new_change_but_retries_committed_request(console_case):
    console, _, setup = console_case
    p = preview(console, setup[4])
    args = action(p)
    first = console.apply(setup[4], **args)
    console.clock = lambda: p["preview_expires_at_ms"] + 1
    assert console.apply(setup[4], **args) == first
    with pytest.raises(ValueError, match="PREVIEW_EXPIRED"):
        console.apply(setup[4], **action(p))


def test_web_change_is_loaded_by_next_pipeline_and_frozen_in_new_decision(
    console_case, pipeline_case
):
    console, _, setup = console_case
    pipeline, *_ = pipeline_case
    scope = pipeline.runtime.scope
    registry = setup[1].enroll(
        console.tenant,
        scope,
        display_name="QA pipeline",
        credential_ref=uuid4(),
        request_id=uuid4(),
    )["registry_id"]
    PolicyStore(console.connect, scope).patch(
        rehearsal_profile("1000"), expected_version=0, reason="QA initial"
    )
    published = console.apply(registry, **action(preview(console, registry)))
    # This pipeline fixture supplies an explicit mock account, not the real
    # BinanceDirectionalAccountContext. Include its remaining stop-risk budget.
    from v2_core.capital_model import portfolio_budget

    account = pipeline_case[2]
    with console.connect() as conn:
        account["sizing"]["remaining_portfolio_risk"] = portfolio_budget(
            conn,
            scope,
            settings=PolicyStore(console.connect, scope).read().values,
            capital={"base": account["sizing"]["balance"], "factor": "1"},
        )["remaining_stop_risk"]
    cycle = pipeline.run_once()
    assert cycle["status"] == "CYCLE_COMPLETE", cycle
    assert cycle["phases"]["configuration"]["version"] == published["version"]
    with console.connect() as c:
        config, snapshot = c.execute(
            "SELECT e.config,e.snapshot FROM v2_trade_intents i JOIN v2_decision_evidence e USING(evidence_ref) WHERE i.account_id=%s",
            (scope.account_id,),
        ).fetchone()
    assert config["policy_version"] == published["version"]
    assert config["policy"]["sizing.max_margin_fraction"] == "0.10"
    assert (
        snapshot["features"]["evaluation"]["sizing_explanation"]["version"]
        == "directional-sizing-v1"
    )
