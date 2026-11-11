import json

import pytest
from test_v2_shared_market import CH, Cache, Public
from test_v2_shared_market import shared as shared_fixture

from services.v2_quant_research import QuantPolicy, QuantResearch
from services.v2_shared_market import IncrementalArchive, SharedMarketHub
from services.v2_trading_health import TradingHealth
from tests.support.v2_admission import database as database_fixture

database = database_fixture
shared = shared_fixture


@pytest.mark.parametrize("failure", [False, True])
def test_shared_hub_bounded_research_does_not_repoll_or_disable_market(shared, failure):
    public = Public(("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT"))
    archive = IncrementalArchive(CH(), "SANDBOX")
    QuantPolicy(mode="SHADOW").save(
        shared, "SANDBOX", expected_version=0, request_key="qa-shadow"
    )
    research = QuantResearch(
        shared, archive=archive, environment="SANDBOX", clock_ms=lambda: public.now
    )
    tick = [0]

    class Budgeted:
        def observe(self, *args, **kwargs):
            tick[0] += 2000
            if failure:
                raise ValueError("QUANT_HISTORY_GAP")
            return research.observe(*args, **kwargs)

    hub = SharedMarketHub(
        shared,
        transport=public,
        cache=Cache(),
        archive=archive,
        clock_ms=lambda: public.now,
        monotonic_ms=lambda: tick[0],
        quant_research=Budgeted(),
    )
    result = hub.run_once()
    assert result["status"] == "CURRENT" and result["fresh"] == 4
    assert result["quant"] == {
        "mode": "SHADOW",
        "elapsed_ms": 2000,
        "failures": int(failure),
        "budget_skips": 3,
    }
    assert len([p for p, _ in public.calls if p.endswith("/klines")]) == 4
    health = TradingHealth(
        shared,
        account_id="test-account",
        quant_enabled=True,
        clock_ms=lambda: public.now,
    )
    assert "QUANT_RESEARCH_DEGRADED" in health()
    saved = json.loads(health.store.read(health.key).payload_json)
    assert saved["findings"]["QUANT_RESEARCH_DEGRADED"]["budget_skips"] == 3
    with shared() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_inbound_signals WHERE source='quant'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.parametrize("fault", ["deleted", "invalid"])
def test_corrupt_research_policy_cannot_stop_legacy_market(shared, fault):
    from v2_core.state import BusinessState

    public = Public()
    archive = IncrementalArchive(CH(), "SANDBOX")
    BusinessState(shared).change(
        QuantPolicy.key("SANDBOX"),
        expected_version=0,
        request_key="qa-invalid-policy",
        payload={} if fault == "deleted" else {"mode": "WRONG"},
        deleted=fault == "deleted",
        reason="QA corrupt policy",
    )
    research = QuantResearch(
        shared, archive=archive, environment="SANDBOX", clock_ms=lambda: public.now
    )
    hub = SharedMarketHub(
        shared,
        transport=public,
        cache=Cache(),
        archive=archive,
        clock_ms=lambda: public.now,
        monotonic_ms=lambda: 0,
        quant_research=research,
    )
    result = hub.run_once()
    assert result["fresh"] == 1 and result["results"][0]["status"] == "CURRENT"
    assert result["quant"]["mode"] == "INVALID"
    assert result["quant"]["failures"] == 1 and result["quant"]["policy_error"]
    assert (
        "QUANT_RESEARCH_DEGRADED"
        in TradingHealth(
            shared,
            account_id="test-account",
            quant_enabled=True,
            clock_ms=lambda: public.now,
        )()
    )
    with shared() as conn:
        assert conn.execute("SELECT count(*) FROM v2_orders").fetchone()[0] == 0
