import pytest
from test_v2_account_risk import configured, prepare, runner
from test_v2_binance import adapter, order
from test_v2_intent_admission import database as database_fixture

from v2_core.transport import ExchangeTransportError

database = database_fixture


class FailingSubmit:
    def __init__(self, error):
        self.error = error
        self.writes = 0

    def __call__(self, method, path, params):
        if method == "GET":
            return {"dualSidePosition": False}
        self.writes += 1
        raise self.error


@pytest.mark.parametrize(
    "code",
    [
        -1021,
        -1022,
        -1100,
        -1101,
        -1102,
        -1103,
        -1111,
        -1115,
        -1116,
        -1117,
        -1121,
        -1130,
    ],
)
def test_synchronous_validation_rejection_is_not_an_unknown_order(code):
    transport = FailingSubmit(
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=400, code=code)
    )
    result = adapter(transport).submit(order())
    assert result.status == "REJECTED"
    assert result.evidence["submission_sent"] is True
    assert result.evidence["transport"]["exchange_code"] == code
    assert transport.writes == 1


@pytest.mark.parametrize(
    "status,code",
    [
        (503, -1111),
        (400, -1007),
        (400, -1006),
        (400, -2010),
        (400, -2013),
        (429, -1003),
        (400, -99999),
        (400, "-1111"),
    ],
)
def test_ambiguous_errors_never_become_rejections(status, code):
    transport = FailingSubmit(
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=status, code=code)
    )
    with pytest.raises(ExchangeTransportError):
        adapter(transport).submit(order())
    assert transport.writes == 1


def test_error_evidence_never_contains_arbitrary_text():
    exc = ExchangeTransportError("secret URL token", status="secret", code=True)
    assert exc.diagnostic_evidence() == {"category": "TRANSPORT_ERROR"}


def test_unknown_keeps_reservation_and_safe_diagnostics(database):
    risk = configured(database)
    _, _, oid, _ = prepare(database)

    def fail(_):
        raise ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=503, code=-1007)

    execution = runner(database, submit=fail)
    assert execution.dispatch(oid) == "UNKNOWN"
    from test_v2_account_risk import SCOPE

    assert risk.usage(SCOPE)["positions"] == 1
    with database() as conn:
        saved = conn.execute(
            "SELECT evidence FROM v2_order_events WHERE order_id=%s AND status='UNKNOWN'",
            (oid,),
        ).fetchone()[0]
    assert saved["transport"] == {
        "category": "EXCHANGE_RESPONSE_ERROR",
        "http_status": 503,
        "exchange_code": -1007,
    }
    assert execution.recover(oid) == "UNKNOWN"


def test_confirmed_rejection_releases_reservation(database):
    from test_v2_account_risk import SCOPE

    from v2_core.binance import BinanceFutures

    risk = configured(database)
    _, _, oid, _ = prepare(database)
    transport = FailingSubmit(
        ExchangeTransportError("EXCHANGE_RESPONSE_ERROR", status=400, code=-1111)
    )
    venue = BinanceFutures(
        transport, account_id=SCOPE.account_id, environment=SCOPE.environment
    )
    assert runner(database, submit=venue.submit).dispatch(oid) == "REJECTED"
    assert risk.usage(SCOPE)["positions"] == 0
    assert runner(database, submit=venue.submit).dispatch(oid) == "REJECTED"
    assert transport.writes == 1
