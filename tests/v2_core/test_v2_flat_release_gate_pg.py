"""Real isolated PostgreSQL release checks; the venue is strictly a fake GET port.

The shared database fixture refuses non-QA sockets and cluster identities. No
cluster is started here, and no deployment, credential-file or live API is used.
"""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_credential_console import add
from test_v2_execution_routes import case, database, request, routes, setup

from scripts.v2_flat_release_gate import FlatGateError, verify_flat
from tests.support.v2_admission import intent
from v2_core.account_inventory import AccountInventory
from v2_core.account_risk import AccountScope
from v2_core.intents import AdmissionCode, IntentStore
from v2_core.orders import Orders

__all__ = ["case", "database", "routes", "setup"]


@pytest.fixture
def flat_gate_pg(routes):
    store, console = routes
    registry = add(console)["registry_id"]
    request(store, console, registry)
    with console.connect() as conn:
        # The trusted controller's persisted state, never an exposed web action.
        conn.execute(
            "INSERT INTO v2_execution_routes(tenant_id,environment,epoch,"
            "target_registry,binding_version,phase,worker_token) "
            "VALUES (%s,'SANDBOX',1,%s,1,'ACTIVE',%s)",
            (console.tenant, registry, str(uuid4())),
        )
        scope = AccountScope(
            *conn.execute(
                "SELECT exchange,account_id,environment,product "
                "FROM v2_tenant_accounts WHERE tenant_id=%s AND registry_id=%s",
                (console.tenant, registry),
            ).fetchone()
        )
    baseline = {
        "tenant": console.tenant,
        "registry": registry,
        "binding": 1,
        "epoch": 1,
        "request_epoch": 1,
        "pending": False,
        "exchange": scope.exchange,
        "account": scope.account_id,
        "product": scope.product,
    }
    raw = {
        "/fapi/v1/positionSide/dual": {"dualSidePosition": False},
        "/fapi/v1/multiAssetsMargin": {"multiAssetsMargin": False},
        "/fapi/v3/positionRisk": [],
        "/fapi/v3/account": {
            "totalWalletBalance": "1000",
            "availableBalance": "1000",
            "totalMarginBalance": "1000",
            "positions": [],
        },
        "/fapi/v1/openOrders": [],
        "/fapi/v1/openAlgoOrders": [],
    }
    gets, queries, transactions = [], [], []

    class FakeRequest:
        account_id, environment = scope.account_id, scope.environment

        def __call__(self, method, path, params):
            assert method == "GET" and params == {}
            gets.append(path)
            return deepcopy(raw[path])

    @contextmanager
    def observed_connect():
        with console.connect() as conn:

            def execute(query, params=None):
                assert query.startswith(("SET", "SELECT")), "read-only gate only"
                queries.append(query)
                result = conn.execute(query, params)
                if query.startswith("SET TRANSACTION"):
                    settings = (
                        conn.execute("SHOW transaction_read_only").fetchone()[0],
                        conn.execute("SHOW transaction_isolation").fetchone()[0],
                    )
                    assert settings == ("on", "repeatable read")
                    transactions.append(settings)
                return result

            yield SimpleNamespace(execute=execute)

    inventory = AccountInventory(FakeRequest(), scope=scope, clock_ms=lambda: 1000)

    def run():
        # The isolated schema owner needs no role switch. The fixed routing role
        # is covered by pure contract tests; no global QA roles/grants are made.
        return verify_flat(
            connect=observed_connect,
            inventory=inventory,
            scope=scope,
            baseline=baseline,
            clock_ms=lambda: 1000,
        )

    return SimpleNamespace(
        run=run,
        connect=console.connect,
        scope=scope,
        baseline=baseline,
        gets=gets,
        queries=queries,
        transactions=transactions,
    )


def admit(gate, **scope_changes):
    item = replace(
        intent(),
        **{
            "exchange": gate.scope.exchange,
            "account_id": gate.scope.account_id,
            "environment": gate.scope.environment,
            "product": gate.scope.product,
            **scope_changes,
        },
    )
    assert IntentStore(gate.connect).admit(item).code is AdmissionCode.ACCEPTED
    return item


def test_real_pg_flat_gate_runs_two_readonly_snapshots_and_complete_inventory(
    flat_gate_pg,
):
    gate = flat_gate_pg
    result = gate.run()
    assert result["status"] == "FLAT_VERIFIED"
    assert result["execution_authorized"] is False
    assert gate.transactions == [("on", "repeatable read")] * 2
    assert len(gate.gets) == 7
    assert gate.gets.count("/fapi/v3/positionRisk") == 2
    assert sum("FROM v2_execution_routes" in query for query in gate.queries) == 2
    assert sum("SELECT count(*) FROM v2_orders" in query for query in gate.queries) == 2


@pytest.mark.parametrize(
    "pending", ["orphan_received", "active_episode", "prepared_order"]
)
def test_real_pg_each_local_pending_class_blocks_before_venue(flat_gate_pg, pending):
    gate = flat_gate_pg
    item = admit(gate)
    if pending != "orphan_received":
        order_id, _ = Orders(gate.connect).prepare(item.intent_id)
        with gate.connect() as conn:
            if pending == "active_episode":
                conn.execute(
                    "UPDATE v2_orders SET status='CANCELLED',version=version+1 "
                    "WHERE order_id=%s",
                    (order_id,),
                )
            else:
                # Inconsistent historical state must still block on the order,
                # independently of whether the episode is already terminal.
                conn.execute(
                    "UPDATE v2_episodes SET status='SETTLED' WHERE episode_id=%s",
                    (item.intent_id,),
                )
    with pytest.raises(FlatGateError, match="^LOCAL_RELEASE_WORK_PENDING$"):
        gate.run()
    assert gate.gets == []


def test_real_pg_settled_partial_intent_is_not_an_orphan(flat_gate_pg):
    gate = flat_gate_pg
    item = admit(gate)
    order_id, _ = Orders(gate.connect).prepare(item.intent_id)
    # Seed the terminal projection of a partial fill followed by cancellation.
    # This test checks release SQL semantics, not the settlement implementation.
    with gate.connect() as conn:
        conn.execute(
            "UPDATE v2_trade_intents SET status='PARTIALLY_FILLED',version=version+1 "
            "WHERE intent_id=%s",
            (item.intent_id,),
        )
        conn.execute(
            "UPDATE v2_orders SET status='CANCELLED',version=version+1 WHERE order_id=%s",
            (order_id,),
        )
        conn.execute(
            "UPDATE v2_episodes SET status='SETTLED' WHERE episode_id=%s",
            (item.intent_id,),
        )
    assert gate.run()["status"] == "FLAT_VERIFIED"


@pytest.mark.parametrize(
    "other_scope",
    [
        {"account_id": "qa-unrelated-account"},
        {"environment": "LIVE"},
        {"product": "SPOT"},
        {"exchange": "QA_OTHER_EXCHANGE"},
    ],
)
def test_real_pg_pending_intent_is_strictly_account_scoped(flat_gate_pg, other_scope):
    gate = flat_gate_pg
    admit(gate, **other_scope)
    assert gate.run()["status"] == "FLAT_VERIFIED"
