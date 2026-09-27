from dataclasses import replace
from uuid import uuid4

import pytest
from test_v2_execution_routes import add, case, database, routes, setup
from test_v2_intent_admission import evidence, intent

from services.v2_dashboard import DashboardData
from v2_core.evidence import EvidenceStore
from v2_core.intents import IntentStore

__all__ = ["case", "database", "routes", "setup"]


def test_dashboard_follows_actual_not_requested_account_and_scopes_details(routes):
    store, console = routes
    a, b = add(console)["registry_id"], add(console)["registry_id"]
    with console.connect() as c:
        aid = c.execute(
            "SELECT account_id FROM v2_tenant_accounts WHERE registry_id=%s", (a,)
        ).fetchone()[0]
    EvidenceStore(console.connect).put(evidence())
    trade = replace(intent(), account_id=aid)
    IntentStore(console.connect).admit(trade)
    data = DashboardData(console.connect, console.tenant)
    with pytest.raises(ValueError, match="NO_CONFIRMED"):
        data.overview()
    with console.connect() as c:
        c.execute(
            "INSERT INTO v2_execution_routes(tenant_id,environment,epoch,target_registry,binding_version,phase,worker_token) VALUES(%s,'SANDBOX',1,%s,1,'ACTIVE',%s)",
            (console.tenant, a, str(uuid4())),
        )
    first = data.overview()
    assert first["execution_account"]["registry_id"] == a
    assert first["summary"]["trade_intents"] == 1
    assert len(first["trades"]) == 1
    assert data.trade(trade.intent_id) is not None
    store.request(
        console.tenant,
        "SANDBOX",
        target_registry=b,
        binding_version=1,
        expected_epoch=1,
        request_id=str(uuid4()),
    )
    assert data.overview()["execution_account"]["registry_id"] == a
    with console.connect() as c:
        c.execute(
            "UPDATE v2_execution_routes SET target_registry=%s,epoch=2 WHERE tenant_id=%s",
            (b, console.tenant),
        )
    second = data.overview()
    assert second["execution_account"]["registry_id"] == b
    assert second["summary"]["trade_intents"] == 0
    assert second["summary"]["realized_pnl"] == "0"
    assert second["trades"] == second["settlements"] == second["health"] == []
    assert second["daily"]["cash"] == second["daily"]["closed_trades"] == []
    assert data.trade(trade.intent_id) is None
    with pytest.raises(ValueError, match="NO_CONFIRMED"):
        DashboardData(console.connect, str(uuid4())).overview()
