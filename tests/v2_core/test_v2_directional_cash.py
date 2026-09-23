from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal

import pytest
from test_v2_directional_admission import formal
from test_v2_directional_replay import SCOPE, scenario
from test_v2_intent_admission import database as database_fixture
from test_v2_trading_pipeline import case as pipeline_fixture

from services.v2_directional_cash import DirectionalCashAudit, reconcile_cash
from v2_core.state import BusinessState, StateKey

database = database_fixture
pipeline_case = pipeline_fixture


@pytest.fixture
def closed(database):
    _, runtime, signal, context = scenario(database)
    result = formal(runtime).consume(signal, context=context)
    episode = result["intent_id"]
    opening = result["order_id"]
    orders, ledger = runtime.data.orders, runtime.data.ledger
    orders.transition(opening, expected_version=1, status="SUBMITTING", evidence={})
    ledger.record_fill(
        order_id=opening,
        exchange_fill_id="1",
        quantity="1.25",
        price="100",
        fee="0.05",
        fee_currency="USDT",
        occurred_at_ms=3,
        evidence={
            "source": "binance-userTrades",
            "trade_id": "1",
            "venue_realized_pnl": "0",
        },
    )
    orders.transition(
        opening,
        expected_version=2,
        status="FILLED",
        exchange_order_id="10",
        evidence={"fills_complete": True},
    )
    closing, _ = orders.prepare(
        episode,
        leg="CLOSE",
        quantity="1.25",
        request_key="qa-close",
        evidence={"source": "qa"},
    )
    orders.transition(closing, expected_version=1, status="SUBMITTING", evidence={})
    ledger.record_fill(
        order_id=closing,
        exchange_fill_id="2",
        quantity="1.25",
        price="92",
        fee="0.046",
        fee_currency="USDT",
        occurred_at_ms=10,
        evidence={
            "source": "binance-userTrades",
            "trade_id": "2",
            "venue_realized_pnl": "-10",
        },
    )
    orders.transition(
        closing,
        expected_version=2,
        status="FILLED",
        exchange_order_id="11",
        evidence={"fills_complete": True},
    )

    class Income:
        account_id, environment = SCOPE.account_id, SCOPE.environment
        calls = 0
        fail = False

        def __init__(self):
            self.rows = [
                {
                    "incomeType": "COMMISSION",
                    "income": "-0.05",
                    "tradeId": "1",
                    "tranId": 1,
                    "time": 3,
                },
                {
                    "incomeType": "COMMISSION",
                    "income": "-0.046",
                    "tradeId": "2",
                    "tranId": 2,
                    "time": 10,
                },
                {
                    "incomeType": "REALIZED_PNL",
                    "income": "-10",
                    "tradeId": "2",
                    "tranId": 3,
                    "time": 10,
                },
                {
                    "incomeType": "FUNDING_FEE",
                    "income": "-0.01",
                    "tradeId": "",
                    "tranId": 4,
                    "time": 5,
                },
            ]

        def __call__(self, method, path, params):
            assert method == "GET" and path == "/fapi/v1/income"
            self.calls += 1
            if self.fail:
                raise TimeoutError("private signed request")
            return [
                {"symbol": "BTCUSDT", "asset": "USDT", **r} for r in deepcopy(self.rows)
            ]

    return runtime, episode, Income()


def auditor(database, income, now=70000):
    return DirectionalCashAudit(database, income, scope=SCOPE, clock_ms=lambda: now)


def test_closed_strategy_to_income_audit_never_pretends_final_settlement(
    database, closed
):
    runtime, episode, income = closed
    proof = auditor(database, income).audit(episode)
    assert proof["status"] == "CASH_MATCHED" and proof["settlement_authorized"] is False
    assert Decimal(proof["provisional_net_pnl"]) == Decimal("-10.106")
    assert Decimal(proof["fees"]) == Decimal("0.096")
    assert Decimal(proof["funding"]) == Decimal("-0.01")
    assert len(proof["funding_candidates"]) == 1
    second = auditor(database, income).audit(episode)
    assert second["income_digest"] == proof["income_digest"]
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_exchange_income").fetchone()[0] == 4
        )
        assert conn.execute("SELECT count(*) FROM v2_settlements").fetchone()[0] == 0
        assert (
            conn.execute("SELECT count(*) FROM v2_cash_adjustments").fetchone()[0] == 0
        )
    assert (
        runtime.data.ledger.report(episode, settlement_currency="USDT")[
            "accounting_status"
        ]
        == "CALCULATED"
    )
    key = StateKey(**asdict(SCOPE), namespace="directional-cash-audit-v1", key=episode)
    assert BusinessState(database).read(key).version == 4


@pytest.mark.parametrize(
    "defect",
    [
        "missing_fee",
        "wrong_fee",
        "wrong_pnl",
        "foreign_symbol",
        "foreign_currency",
        "external_cash",
        "ambiguous_funding",
        "funding_after_close",
        "duplicate_page",
    ],
)
def test_bad_income_cannot_be_called_reconciled(database, closed, defect):
    _, episode, income = closed
    if defect == "missing_fee":
        income.rows.pop(0)
    elif defect == "wrong_fee":
        income.rows[0]["income"] = "-0.1"
    elif defect == "wrong_pnl":
        income.rows[2]["income"] = "-9"
    elif defect == "foreign_symbol":
        income.rows[0]["symbol"] = "ETHUSDT"
    elif defect == "foreign_currency":
        income.rows[0]["asset"] = "BNB"
    elif defect == "external_cash":
        income.rows[0]["incomeType"] = "TRANSFER"
    elif defect == "ambiguous_funding":
        income.rows[3]["time"] = 3
    elif defect == "funding_after_close":
        income.rows[3]["time"] = 11
    else:
        income.rows.append(deepcopy(income.rows[0]))
    try:
        result = auditor(database, income).audit(episode)
    except ValueError:
        pass
    else:
        assert result["status"] == "INCOME_INCOMPLETE"
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_settlements").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT payload->>'status' FROM v2_business_state WHERE scope->>'namespace'='directional-cash-audit-v1'"
            ).fetchone()[0]
            != "CASH_MATCHED"
        )


def test_income_lag_wait_has_no_exchange_calls(database, closed):
    _, episode, income = closed
    assert (
        auditor(database, income, now=60009).audit(episode)["status"]
        == "WAITING_INCOME_LAG"
    )
    assert income.calls == 0


def test_income_second_flooring_still_matches_the_same_trade_ids(database, closed):
    _, episode, income = closed
    income.rows[0]["time"] = 2
    income.rows[1]["time"] = 9
    income.rows[2]["time"] = 9
    proof = auditor(database, income).audit(episode)
    assert proof["status"] == "CASH_MATCHED"
    assert Decimal(proof["provisional_net_pnl"]) == Decimal("-10.106")


def test_income_transport_failure_is_nonterminal_and_sanitized(database, closed):
    _, episode, income = closed
    income.fail = True
    result = auditor(database, income).audit(episode)
    assert result["status"] == "INCOME_INCOMPLETE"
    assert result["run"]["error_code"] == "TimeoutError"
    assert "private" not in str(result)


def test_foreign_account_rejected_before_network(database, closed):
    _, episode, income = closed
    service = auditor(database, income)
    service.scope = replace(SCOPE, account_id="other")
    with pytest.raises(ValueError, match="CASH_SCOPE_MISMATCH"):
        service.audit(episode)
    assert income.calls == 0


def test_late_income_is_reimported_and_rejected_not_silently_ignored(database, closed):
    _, episode, income = closed
    assert auditor(database, income).audit(episode)["status"] == "CASH_MATCHED"
    income.rows.append(
        {"incomeType": "TRANSFER", "income": "1", "tradeId": "", "tranId": 5, "time": 6}
    )
    with pytest.raises(ValueError, match="UNATTRIBUTED_CASH"):
        auditor(database, income).audit(episode)
    key = StateKey(**asdict(SCOPE), namespace="directional-cash-audit-v1", key=episode)
    assert '"status":"BLOCKED"' in BusinessState(database).read(key).payload_json


def test_cash_match_blocks_pipeline_until_final_account_settlement(database, closed):
    _, episode, income = closed
    result = auditor(database, income).run_once()
    assert result["status"] == "BLOCKED"
    assert result["reason"] == "ACCOUNT_SETTLEMENT_REQUIRED"
    assert result["audits"][episode]["status"] == "CASH_MATCHED"


def test_no_closed_episodes_is_clear_without_income_calls(database):
    class Income:
        account_id, environment = SCOPE.account_id, SCOPE.environment

        def __call__(self, *_):
            pytest.fail("empty stage must not call venue")

    assert auditor(database, Income()).run_once()["status"] == "CLEAR"


def test_account_lock_prevents_income_import(database, closed):
    _, episode, income = closed
    with database() as conn:
        conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s,0))",
            ("testnet-protocol-account:" + SCOPE.key,),
        )
        conn.commit()
        assert auditor(database, income).audit(episode)["status"] == "BUSY"
    assert income.calls == 0


def test_original_fill_realized_pnl_must_match_price_ledger(database, closed):
    runtime, episode, income = closed
    proof = auditor(database, income).audit(episode)
    trace = runtime.data.trace(episode)
    trace["fills"][0]["evidence"]["venue_realized_pnl"] = "1"
    with pytest.raises(ValueError):
        reconcile_cash(
            trace,
            runtime.data.ledger.report(episode, settlement_currency="USDT"),
            [],
            scope=SCOPE,
        )
    assert proof["settlement_authorized"] is False


def test_overlapping_account_episode_never_assigns_funding(database, closed):
    from test_v2_directional_lifecycle import opening

    _, episode, income = closed
    opening(database, strategy="S8", symbol="ETHUSDT")
    with pytest.raises(ValueError, match="OVERLAPPING_ACCOUNT_EPISODE"):
        auditor(database, income).audit(episode)


def test_fact_change_during_import_invalidates_observation(database, closed):
    runtime, episode, income = closed
    service = auditor(database, income)
    original = service.importer.request

    def changed(*args):
        rows = original(*args)
        runtime.data.ledger.adjustment(
            episode_id=episode,
            source_id="qa-concurrent",
            amount_text="1",
            currency="USDT",
            kind="CORRECTION",
            occurred_at_ms=9,
            evidence={"source": "QA"},
        )
        return rows

    service.importer.request = changed
    with pytest.raises(ValueError, match="CASH_FACTS_CHANGED"):
        service.audit(episode)


def test_real_cash_stage_blocks_new_admission_in_unified_cycle(
    database, closed, pipeline_case
):
    _, episode, income = closed
    pipeline, _, _, _, _ = pipeline_case
    pipeline.settlement = auditor(database, income)
    result = pipeline.run_once()
    assert result["status"] == "ENTRY_BLOCKED"
    assert result["phases"]["settlement"]["audits"][episode]["status"] == "CASH_MATCHED"
    assert "s6" not in result["phases"] and "s8" not in result["phases"]
    assert result["entries"] == {}


def test_crash_after_import_started_leaves_no_old_positive_head(database, closed):
    _, episode, income = closed
    service = auditor(database, income)
    assert service.audit(episode)["status"] == "CASH_MATCHED"

    def crash(**_):
        raise KeyboardInterrupt()

    service.importer.import_window = crash
    with pytest.raises(KeyboardInterrupt):
        service.audit(episode)
    key = StateKey(**asdict(SCOPE), namespace="directional-cash-audit-v1", key=episode)
    assert '"status":"AUDITING"' in BusinessState(database).read(key).payload_json
