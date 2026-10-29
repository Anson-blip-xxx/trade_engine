"""Pure final-write fence tests: no database, HTTP, credentials or order writes."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier

import pytest

from v2_core.errors import SubmissionNotSent
from v2_core.reduction_fence import reduction_fence, revalidate_reduction_write

FAILURE = "REDUCE_ONLY_REVALIDATION_FAILED"


def order(**changes):
    return {
        "order_id": "73648960-334f-4e82-aadc-55800292caf0",
        "client_order_id": "v273648960334f4e82aadc55800292caf0",
        "exchange": "BINANCE",
        "account_id": "qa-reduction-fence",
        "environment": "SANDBOX",
        "product": "FUTURES",
        "leg": "CLOSE",
        "reduce_only": True,
        "symbol": "BTCUSDT",
        "side": "SELL",
        "quantity": "1.250000000000000001",
        "order_type": "MARKET",
        "limit_price": None,
        "time_in_force": None,
        "request_evidence": {"revision": 12},
        **changes,
    }


def wire(original):
    params = {
        "symbol": original["symbol"],
        "side": original["side"],
        "positionSide": "BOTH",
        "type": original["order_type"],
        "quantity": original["quantity"],
        "newClientOrderId": original["client_order_id"],
        "newOrderRespType": "RESULT",
        "reduceOnly": "true",
    }
    if original["order_type"] == "LIMIT":
        params.update(
            price=original["limit_price"], timeInForce=original["time_in_force"]
        )
    return params


def validate(original, params=None, **changes):
    args = {
        "account_id": original["account_id"],
        "environment": original["environment"],
        "method": "POST",
        "path": "/fapi/v1/order",
        "params": wire(original) if params is None else params,
        **changes,
    }
    return revalidate_reduction_write(**args)


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE", "malformed"])
def test_without_fence_no_new_authority_or_behavior(method):
    assert revalidate_reduction_write(None, None, method, None, None) is None


def test_get_does_not_invoke_callback_or_require_write_identity():
    with reduction_fence(order(), lambda _: pytest.fail("GET invoked check")):
        assert revalidate_reduction_write("other", "LIVE", "GET", "/read", {}) is None


@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("environment", ["SANDBOX", "LIVE"])
def test_each_matching_boundary_revalidates_and_returns_no_permit(side, environment):
    original = order(side=side, environment=environment)
    calls = []

    def check(snapshot):
        calls.append(snapshot)
        return True

    with reduction_fence(original, check):
        for _ in range(3):
            assert validate(original) is None
    assert calls == [original] * 3
    assert all(snapshot is not original for snapshot in calls)


@pytest.mark.parametrize("time_in_force", ["GTC", "IOC", "FOK"])
def test_limit_terms_are_bound_using_exact_decimal_equality(time_in_force):
    original = order(
        order_type="LIMIT", limit_price="12345.1000", time_in_force=time_in_force
    )
    params = wire(original)
    params["price"] = "12345.1"
    with reduction_fence(original, lambda _: True):
        assert validate(original, params) is None


@pytest.mark.parametrize(
    "change",
    [
        {"price": "12345.100000000000000001"},
        {"price": 12345.1},
        {"price": "NaN"},
        {"timeInForce": "IOC"},
        {"type": "MARKET"},
    ],
)
def test_limit_price_and_time_in_force_cannot_change(change):
    original = order(order_type="LIMIT", limit_price="12345.1", time_in_force="GTC")
    with (
        reduction_fence(
            original, lambda _: pytest.fail("limit mutation invoked check")
        ),
        pytest.raises(SubmissionNotSent, match="^" + FAILURE + "$"),
    ):
        validate(original, {**wire(original), **change})


def test_decimal_wire_formatting_is_not_a_quantity_change():
    original = order(quantity="1.2500")
    with reduction_fence(original, lambda _: True):
        assert validate(original, {**wire(original), "quantity": "1.25"}) is None


@pytest.mark.parametrize(
    "change",
    [
        {"account_id": "other"},
        {"environment": "LIVE"},
        {"method": "DELETE"},
        {"method": "PUT"},
        {"method": "post"},
        {"path": "/fapi/v1/algoOrder"},
        {"path": "/fapi/v1/order?secret=never-store"},
        {"params": []},
        {"params": {}},
    ],
)
def test_other_write_never_reaches_callback(change):
    original = order()
    with (
        reduction_fence(original, lambda _: pytest.fail("mismatch invoked check")),
        pytest.raises(SubmissionNotSent, match="^" + FAILURE + "$"),
    ):
        validate(original, **change)


@pytest.mark.parametrize(
    "field,value",
    [
        ("symbol", "ETHUSDT"),
        ("side", "BUY"),
        ("positionSide", "LONG"),
        ("type", "LIMIT"),
        ("quantity", "1.250000000000000002"),
        ("quantity", "1.25"),
        ("quantity", 1.25),
        ("quantity", True),
        ("quantity", "NaN"),
        ("quantity", "-1"),
        ("quantity", "0"),
        ("newClientOrderId", "other-order"),
        ("newOrderRespType", "ACK"),
        ("reduceOnly", "false"),
        ("reduceOnly", True),
        ("reduceOnly", 1),
    ],
)
def test_exact_wire_terms_cannot_change(field, value):
    original = order()
    params = wire(original)
    params[field] = value
    with (
        reduction_fence(original, lambda _: pytest.fail("mismatch invoked check")),
        pytest.raises(SubmissionNotSent, match="^" + FAILURE + "$"),
    ):
        validate(original, params)


@pytest.mark.parametrize("field", list(wire(order())))
def test_missing_wire_term_is_rejected(field):
    original = order()
    params = wire(original)
    del params[field]
    with reduction_fence(original, lambda _: True), pytest.raises(SubmissionNotSent):
        validate(original, params)


@pytest.mark.parametrize(
    "field",
    ["closePosition", "price", "timeInForce", "priceMatch", "timestamp", "signature"],
)
def test_extra_wire_terms_cannot_change_the_bound_write(field):
    original = order()
    params = {**wire(original), field: "never-store-secret"}
    with (
        reduction_fence(original, lambda _: True),
        pytest.raises(SubmissionNotSent, match="^" + FAILURE + "$") as raised,
    ):
        validate(original, params)
    assert "never-store-secret" not in str(raised.value)


@pytest.mark.parametrize(
    "change",
    [
        {"leg": "OPEN"},
        {"reduce_only": False},
        {"reduce_only": "true"},
        {"reduce_only": 1},
        {"exchange": "OTHER"},
        {"product": "SPOT"},
        {"environment": "PRODUCTION"},
        {"account_id": ""},
        {"account_id": "contains whitespace"},
        {"symbol": "BTC/USDT"},
        {"side": "SHORT"},
        {"quantity": 1.25},
        {"quantity": "0"},
        {"quantity": "NaN"},
        {"client_order_id": ""},
        {"client_order_id": "x" * 37},
        {"client_order_id": "\nsecret"},
        {"order_type": "STOP_MARKET"},
        {"limit_price": "1"},
        {"time_in_force": "GTC"},
        {"order_type": "LIMIT", "limit_price": "1", "time_in_force": "GTX"},
        {"order_type": "LIMIT", "limit_price": "-1", "time_in_force": "GTC"},
        {"order_type": "LIMIT", "limit_price": "1", "time_in_force": None},
    ],
)
def test_invalid_original_binding_is_fixed_unsent_error_and_leaves_no_context(change):
    with (
        pytest.raises(SubmissionNotSent, match="^" + FAILURE + "$"),
        reduction_fence(order(**change), lambda _: True),
    ):
        pytest.fail("invalid fence installed")
    assert revalidate_reduction_write(None, None, "POST", None, None) is None


@pytest.mark.parametrize("callback", [None, False, 1, {}])
def test_noncallable_check_cannot_install_fence(callback):
    with (
        pytest.raises(SubmissionNotSent, match="^" + FAILURE + "$"),
        reduction_fence(order(), callback),
    ):
        pytest.fail("invalid check installed")


@pytest.mark.parametrize("value", [False, None, 1, {"allowed": True}])
def test_only_literal_true_accepts_current_evidence(value):
    original = order()
    with (
        reduction_fence(original, lambda _: value),
        pytest.raises(SubmissionNotSent, match="^" + FAILURE + "$"),
    ):
        validate(original)


def test_callback_error_is_sanitized_and_context_resets_after_failure():
    original = order()

    def broken(_):
        raise RuntimeError("secret key, URL, raw response must never escape")

    with (
        pytest.raises(SubmissionNotSent, match="^" + FAILURE + "$") as raised,
        reduction_fence(original, broken),
    ):
        validate(original)
    assert raised.value.__suppress_context__ is True
    assert revalidate_reduction_write(None, None, "POST", None, None) is None
    with reduction_fence(original, lambda _: True):
        assert validate(original) is None


def test_body_exception_resets_fence_without_reclassifying_possible_write():
    with (
        pytest.raises(TimeoutError, match="after actual POST"),
        reduction_fence(order(), lambda _: True),
    ):
        raise TimeoutError("after actual POST")
    assert revalidate_reduction_write(None, None, "POST", None, None) is None


def test_nested_fence_rejected_without_replacing_outer_identity():
    original = order()
    with reduction_fence(original, lambda _: True):
        with (
            pytest.raises(SubmissionNotSent),
            reduction_fence(order(account_id="other"), lambda _: True),
        ):
            pytest.fail("nested fence replaced original")
        assert validate(original) is None
        with pytest.raises(SubmissionNotSent):
            validate(original, account_id="other")


def test_original_and_callback_mutation_cannot_change_bound_identity():
    original = order()
    initial = deepcopy(original)
    seen = []

    def check(snapshot):
        seen.append(deepcopy(snapshot))
        snapshot["quantity"] = "999"
        snapshot["request_evidence"]["revision"] = 999
        return True

    with reduction_fence(original, check):
        original["quantity"] = "999"
        original["request_evidence"]["revision"] = 999
        assert validate(initial) is None
        assert validate(initial) is None
        with pytest.raises(SubmissionNotSent):
            validate(original)
    assert seen == [initial, initial]


def test_check_cannot_recursively_submit_a_write():
    original = order()

    def check(_):
        validate(original)
        return True

    with (
        reduction_fence(original, check),
        pytest.raises(SubmissionNotSent, match="^" + FAILURE + "$"),
    ):
        validate(original)


def test_request_contexts_are_isolated_between_concurrent_threads():
    barrier = Barrier(3)

    def worker(index):
        original = order(
            account_id="account-" + str(index), client_order_id="close-" + str(index)
        )
        seen = []

        def check(snapshot):
            seen.append(snapshot["account_id"])
            return True

        with reduction_fence(original, check):
            barrier.wait(timeout=5)
            assert validate(original) is None
            with pytest.raises(SubmissionNotSent):
                validate(original, account_id="account-" + str(1 - index))
            barrier.wait(timeout=5)
        assert revalidate_reduction_write(None, None, "POST", None, None) is None
        return seen

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = (pool.submit(worker, index) for index in range(2))
        barrier.wait(timeout=5)
        assert revalidate_reduction_write(None, None, "POST", None, None) is None
        barrier.wait(timeout=5)
        assert first.result(timeout=5) == ["account-0"]
        assert second.result(timeout=5) == ["account-1"]
