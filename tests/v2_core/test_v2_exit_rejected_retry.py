from test_v2_directional_exit_stage import database, observation, setup_exit

from v2_core.runner import ExchangeObservation

__all__ = ["database"]


def test_rejected_exit_retries_only_after_fresh_observation(database):
    runtime, result, venue, service = setup_exit(
        database, allow_writes=True, mark_price="105"
    )
    submitted = []

    def reject(order):
        submitted.append(order["client_order_id"])
        return ExchangeObservation(
            order["client_order_id"],
            "REJECTED",
            evidence={"reason": "QA_CONFIRMED_REJECT"},
        )

    original = runtime.execution.submit
    runtime.execution.submit = reject
    service.run_once()
    again = service.run_once()
    assert len(submitted) == 1
    assert (
        again["exits"][result["intent_id"]]["reason"]
        == "REJECTED_EXIT_AWAITING_FRESH_OBSERVATION"
    )
    runtime.execution.submit = original
    service.clock = lambda: 600005
    service.observe = lambda _: observation(600005, mark_price="105")
    answer = service.run_once()
    assert answer["exits"][result["intent_id"]]["outcome"] == "ACKNOWLEDGED", answer
    assert len(venue.order_writes) == 1
    assert venue.order_writes[0]["newClientOrderId"] != submitted[0]
    service.run_once()
    assert len(venue.order_writes) == 1
    venue.close.update(status="FILLED", executedQty="0.625")
    venue.trades = [
        {
            "id": 901,
            "orderId": 900,
            "symbol": "BTCUSDT",
            "side": "SELL",
            "positionSide": "BOTH",
            "qty": "0.625",
            "price": "105",
            "commission": "0.02625",
            "commissionAsset": "USDT",
            "time": 600006,
            "realizedPnl": "3.125",
        }
    ]
    venue.rows["/fapi/v3/positionRisk"][0]["positionAmt"] = "0.625"
    answer = service.run_once()
    assert answer["exits"][result["intent_id"]]["status"] == "WAIT", answer
    assert len(venue.order_writes) == 1
