"""Bounded account facts through real transport classes with in-memory HTTP/PG."""

import json
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from decimal import Decimal, Inexact, localcontext
from fractions import Fraction
from types import SimpleNamespace

import pytest

import services.v2_strategy_account_facts as module
from services.v2_strategy_account_facts import SandboxStrategyAccountFacts
from services.v2_strategy_market_reader import ClosedMarketBars
from tests.v2_core.test_v2_strategy_candidate_builder import bars, scene
from tests.v2_core.test_v2_strategy_sizing import run_size, sizing_scene
from v2_core.evidence import canonical, digest
from v2_core.public_market import BinancePublicMarket
from v2_core.runtime_policy import resolve
from v2_core.transport import BinanceSignedTransport


class Clock:
    def __init__(self, value):
        self.value = value
        self.elapsed = 0

    def now(self):
        return self.value

    def monotonic(self):
        return self.elapsed / 1000

    def advance(self, milliseconds):
        self.value += milliseconds
        self.elapsed += milliseconds


class MemoryConnection:
    autocommit = False

    def __init__(self, owner):
        self.owner = owner

    def execute(self, sql, params=()):
        self.owner.sql.append((sql, params))
        assert not sql.startswith(("INSERT", "UPDATE", "DELETE", "TRUNCATE", "ALTER"))
        return self

    def fetchall(self):
        return deepcopy(self.owner.held)


class MemoryHTTP:
    def __init__(self, owner, host, timeout):
        self.owner, self.host, self.timeout = owner, host, timeout

    def request(self, method, target, body=None, headers=None):
        assert method == "GET"
        assert self.host == "demo-fapi.binance.com"
        assert self.timeout == 3
        self.path = target.split("?", 1)[0]
        self.owner.http_calls.append((method, self.path))
        if self.owner.on_request:
            self.owner.on_request(self.path)

    def getresponse(self):
        response = deepcopy(self.owner.responses[self.path])
        if callable(response):
            response = response()
        return SimpleNamespace(
            status=200,
            read=lambda _: json.dumps(response).encode(),
            getheader=lambda *args: None,
        )

    def close(self):
        pass


class Harness:
    def __init__(self, monkeypatch, *, side="BUY"):
        self.decision, self.binding, self.policy, _ = sizing_scene(side=side)
        _, self.state, *_ = scene(self.decision)
        self.frame = ClosedMarketBars(
            "BTCUSDT",
            "SANDBOX",
            self.decision.observed_at_ms,
            self.decision.available_at_ms,
            json.dumps(bars(side=side)),
        )
        self.clock = Clock(self.decision.decided_at_ms)
        self.sql, self.http_calls, self.held, self.health_calls = [], [], [], []
        self.on_request = None
        self.stop = SimpleNamespace(stopped=False)
        self.stop.is_set = lambda: self.stop.stopped
        self.settings = resolve(
            {
                "capital.enabled": True,
                "capital.model_enabled": True,
                "capital.reference_wallet": "1000",
                "portfolio_risk.enabled": True,
                "capital.margin_buffer": "1.10",
                "capital.pool_fraction": ".70",
                "sizing.pool_fraction": ".70",
                "leverage.adaptive_enabled": True,
                "entry.force_isolated": True,
                "leverage.low": 2,
                "leverage.medium": 3,
                "leverage.adaptive_high": 5,
            }
        )
        self.profile_reads = 0
        self.change_profile = False
        self.catalog_error = None
        self.health = {"base": "1000", "factor": "1"}
        self.budget = {"remaining_stop_risk": "30"}
        filters = [
            {
                "filterType": "MARKET_LOT_SIZE",
                "minQty": ".001",
                "maxQty": "100000",
                "stepSize": ".001",
            },
            {
                "filterType": "PRICE_FILTER",
                "minPrice": ".000001",
                "maxPrice": "1000000",
                "tickSize": ".01",
            },
            {"filterType": "MIN_NOTIONAL", "notional": "5"},
        ]
        row = {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
            "filters": filters,
            "supported": True,
        }
        self.catalog = {
            "observed_at_ms": self.clock.now(),
            "contracts": {"BTCUSDT": row},
            "digest": digest(canonical({"BTCUSDT": row})),
        }
        price = Decimal(self.decision.structure["values"]["close"])
        self.responses = {
            "/fapi/v3/positionRisk": [],
            "/fapi/v3/account": {
                "totalWalletBalance": "1000",
                "totalMarginBalance": "1000",
                "availableBalance": "850",
                "totalInitialMargin": "150",
                "totalPositionInitialMargin": "100",
                "totalOpenOrderInitialMargin": "50",
                "assets": [
                    {
                        "asset": "USDT",
                        "walletBalance": "1000",
                        "marginBalance": "1000",
                        "availableBalance": "850",
                        "initialMargin": "150",
                        "positionInitialMargin": "100",
                        "openOrderInitialMargin": "50",
                    }
                ],
            },
            "/fapi/v1/accountConfig": {
                "canTrade": True,
                "dualSidePosition": False,
                "multiAssetsMargin": False,
            },
            "/fapi/v1/symbolConfig": [
                {"symbol": "BTCUSDT", "maxNotionalValue": "1000000"}
            ],
            "/fapi/v1/leverageBracket": [
                {
                    "symbol": "BTCUSDT",
                    "brackets": [
                        {
                            "notionalFloor": 0,
                            "notionalCap": 1000000,
                            "initialLeverage": 20,
                            "maintMarginRatio": 0.005,
                        }
                    ],
                }
            ],
            "/fapi/v1/premiumIndex": {
                "symbol": "BTCUSDT",
                "markPrice": str(price),
                "time": self.clock.now(),
            },
            "/fapi/v1/ticker/bookTicker": {
                "symbol": "BTCUSDT",
                "bidPrice": str(price - Decimal(".01")),
                "askPrice": str(price + Decimal(".01")),
                "bidQty": "1000",
                "askQty": "1000",
                "time": self.clock.now(),
            },
            "/fapi/v1/exchangeInfo": {"symbols": [row]},
        }
        self.request = BinanceSignedTransport(
            account_id=self.binding.scope.account_id,
            environment="SANDBOX",
            api_key="dummy-test-key",
            api_secret="dummy-test-secret",
            clock_ms=self.clock.now,
            permit=lambda *args: True,
            timeout=3,
            connection_factory=self.http,
        )
        self.public = BinancePublicMarket(
            environment="SANDBOX",
            enabled=True,
            timeout=3,
            budget=SimpleNamespace(permit=lambda *_: True),
            connection_factory=self.http,
        )

        owner = self

        class Store:
            def __init__(self, connect, scope):
                assert scope == owner.binding.scope

            def read(self):
                owner.profile_reads += 1
                return SimpleNamespace(
                    values=deepcopy(owner.settings),
                    version=1,
                    digest="changed"
                    if owner.change_profile and owner.profile_reads > 1
                    else "a" * 64,
                )

        class Catalog:
            def __init__(self, connect, environment, clock):
                assert environment == "SANDBOX"

            def snapshot(self, max_age_ms):
                if owner.catalog_error:
                    raise ValueError(owner.catalog_error)
                assert max_age_ms == owner.policy.max_fact_age_ms
                return deepcopy(owner.catalog)

        def health(connect, scope, account, settings, *, now_ms):
            owner.health_calls.append((scope, deepcopy(account), now_ms))
            return deepcopy(owner.health)

        monkeypatch.setattr(module, "PolicyStore", Store)
        monkeypatch.setattr(module, "Catalog", Catalog)
        monkeypatch.setattr(module, "current_health", health)
        monkeypatch.setattr(
            module,
            "portfolio_budget",
            lambda conn, scope, **kwargs: deepcopy(owner.budget),
        )

    def http(self, host, timeout):
        return MemoryHTTP(self, host, timeout)

    @contextmanager
    def connect(self):
        yield MemoryConnection(self)

    def provider(self, **changes):
        return SandboxStrategyAccountFacts(
            self.connect,
            self.request,
            self.public,
            **{
                "scope": self.binding.scope,
                "clock_ms": self.clock.now,
                "selected_leverage": 3,
                "read_timeout_ms": 3000,
                "collection_timeout_ms": 25000,
                "pg_statement_timeout_ms": 1000,
                "stop": self.stop,
                "monotonic": self.clock.monotonic,
                **changes,
            },
        )

    def collect(self, provider=None):
        return (provider or self.provider())(self.state, self.binding, self.frame)


@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_real_readonly_transports_to_typed_sizing_and_true_capital_snapshot(
    monkeypatch, side
):
    h = Harness(monkeypatch, side=side)
    provider = h.provider()
    facts = h.collect(provider)
    assert facts.scope == h.binding.scope and facts.margin_buffer == "1.1"
    assert facts.used_margin == "150" and facts.available_margin == "850"
    assert facts.pending_reserved_margin == "0" and facts.remaining_stop_risk == "30"
    assert facts.entry_price == str(
        Decimal(
            h.responses["/fapi/v1/ticker/bookTicker"][
                "askPrice" if side == "BUY" else "bidPrice"
            ]
        )
    )
    assert facts.leverage_proof["checks"] == {}
    assert (
        facts.leverage_proof["capital_context"]["pending_mode"]
        == "ALL_HELD_FULL_UPPER_BOUND"
    )
    assert all(method == "GET" for method, _ in h.http_calls)
    assert len(h.http_calls) == 8
    assert not any(path == "/fapi/v1/exchangeInfo" for _, path in h.http_calls)
    assert h.sql[0][0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert sum("SET TRANSACTION" in sql for sql, _ in h.sql) == 2
    assert any("statement_timeout='1000ms'" in sql for sql, _ in h.sql)
    result = run_size((h.decision, h.binding, h.policy, facts))
    assert result.status == "SIZED", result.reason
    snapshot = provider.last_verified_account
    assert snapshot.account["totalWalletBalance"] == "1000"
    assert snapshot.account["totalInitialMargin"] == "150"
    assert snapshot.account == h.health_calls[0][1]
    snapshot.account["totalWalletBalance"] = "999999"
    assert provider.last_verified_account.account["totalWalletBalance"] == "1000"


def test_model_capital_does_not_replace_real_wallet_in_publishable_snapshot(
    monkeypatch,
):
    h = Harness(monkeypatch)
    h.health = {"base": "500", "factor": ".5"}
    provider = h.provider()
    assert h.collect(provider).capital_base == "500"
    assert provider.last_verified_account.account["totalWalletBalance"] == "1000"


def test_all_held_including_unknown_reservations_are_counted_with_buffer_and_original_floor(
    monkeypatch,
):
    h = Harness(monkeypatch)
    h.held = [
        (
            "90",
            "3",
            None,
            {"reserved_margin": "35"},
            "proof-one",
            asdict(h.binding.scope),
        ),
        ("120", "3", None, None, "proof-two", asdict(h.binding.scope)),
    ]
    facts = h.collect()
    assert facts.pending_reserved_margin == "79"  # max(30*1.1,35) + 40*1.1
    context = facts.leverage_proof["capital_context"]
    assert (
        context["held_count"] == 2
        and context["unknown_reservations_not_dropped"] is True
    )
    sql = next(sql for sql, _ in h.sql if "FROM v2_risk_reservations" in sql)
    assert "status='HELD'" in sql and "v2_orders" not in sql


@pytest.mark.parametrize(
    "defect",
    ["missing_evidence", "foreign_scope", "missing_leverage", "invalid_original"],
)
def test_missing_or_foreign_reservation_proof_never_becomes_zero(monkeypatch, defect):
    h = Harness(monkeypatch)
    row = ["90", "3", None, {"reserved_margin": "35"}, "proof", asdict(h.binding.scope)]
    if defect == "missing_evidence":
        row[4] = None
    elif defect == "foreign_scope":
        row[5]["account_id"] = "someone-else"
    elif defect == "missing_leverage":
        row[1] = None
    else:
        row[3] = {"reserved_margin": "NaN"}
    h.held = [tuple(row)]
    provider = h.provider()
    with pytest.raises(ValueError):
        h.collect(provider)
    assert provider.last_verified_account is None


@pytest.mark.parametrize(
    "flag", ["_trading", "_cancel", "_order_cancel", "_protection", "_settings"]
)
def test_any_signed_write_capability_is_rejected_before_io(monkeypatch, flag):
    h = Harness(monkeypatch)
    setattr(h.request, flag, True)
    with pytest.raises(ValueError, match="READ_ONLY_TRANSPORT"):
        h.provider()
    assert not h.http_calls and not h.sql


@pytest.mark.parametrize(
    "change",
    [
        "account",
        "live_signed",
        "live_public",
        "timeout_signed",
        "timeout_public",
        "disabled_public",
        "fake_callable",
    ],
)
def test_transport_scope_and_actual_class_timeout_are_required(monkeypatch, change):
    h = Harness(monkeypatch)
    if change == "account":
        h.request.account_id = "foreign"
    elif change == "live_signed":
        h.request.environment = "LIVE"
    elif change == "live_public":
        h.public.environment = "LIVE"
    elif change == "timeout_signed":
        h.request._timeout = 10
    elif change == "timeout_public":
        h.public.timeout = 10
    elif change == "disabled_public":
        h.public.enabled = False
    else:
        h.request = lambda *args: None
    with pytest.raises(ValueError, match="READ_ONLY_TRANSPORT"):
        h.provider()


@pytest.mark.parametrize("leverage", [1, 4, 8, True, "3"])
def test_no_eight_x_without_independent_boost_facts(monkeypatch, leverage):
    h = Harness(monkeypatch)
    with pytest.raises(ValueError, match="EXPLICIT_LOW_LEVERAGE"):
        h.provider(selected_leverage=leverage)


def test_readonly_capability_is_revalidated_after_construction(monkeypatch):
    h = Harness(monkeypatch)
    provider = h.provider()
    h.request._settings = True
    with pytest.raises(ValueError, match="READ_ONLY_TRANSPORT"):
        h.collect(provider)
    assert not h.http_calls


@pytest.mark.parametrize(
    "name,value",
    [("canTrade", False), ("dualSidePosition", True), ("multiAssetsMargin", True)],
)
def test_unverified_account_modes_fail_closed(monkeypatch, name, value):
    h = Harness(monkeypatch)
    h.responses["/fapi/v1/accountConfig"][name] = value
    with pytest.raises(ValueError, match="STRATEGY_FACTS_"):
        h.collect()


@pytest.mark.parametrize(
    "defect",
    [
        "foreign_collateral",
        "global_mismatch",
        "missing_asset",
        "duplicate_asset",
        "margin_sum",
        "negative",
        "float_amount",
    ],
)
def test_usdt_cash_facts_never_mix_other_collateral_or_guess_missing_values(
    monkeypatch, defect
):
    h = Harness(monkeypatch)
    account = h.responses["/fapi/v3/account"]
    if defect == "foreign_collateral":
        account["assets"].append(
            {"asset": "USDC", "walletBalance": "1", "initialMargin": "0"}
        )
    elif defect == "global_mismatch":
        account["totalWalletBalance"] = "1001"
    elif defect == "missing_asset":
        account["assets"] = []
    elif defect == "duplicate_asset":
        account["assets"] *= 2
    elif defect == "margin_sum":
        account["totalInitialMargin"] = account["assets"][0]["initialMargin"] = "100"
    elif defect == "negative":
        account["availableBalance"] = account["assets"][0]["availableBalance"] = "-1"
    else:
        account["totalWalletBalance"] = 1000.0
    with pytest.raises(ValueError):
        h.collect()


def test_fresh_catalog_is_reused_and_stale_catalog_fetch_keeps_real_read_timestamp(
    monkeypatch,
):
    h = Harness(monkeypatch)
    h.catalog_error = "CONTRACT_CATALOG_STALE"
    result = h.collect()
    assert ("GET", "/fapi/v1/exchangeInfo") in h.http_calls
    assert result.filters_at_ms == h.clock.now()


def test_corrupt_catalog_does_not_silently_fallback(monkeypatch):
    h = Harness(monkeypatch)
    h.catalog_error = "CONTRACT_CATALOG_CORRUPT"
    with pytest.raises(ValueError, match="CONTRACT_CATALOG_CORRUPT"):
        h.collect()
    assert not h.http_calls


@pytest.mark.parametrize(
    "path", ["/fapi/v1/premiumIndex", "/fapi/v1/ticker/bookTicker"]
)
@pytest.mark.parametrize("delta", [-1001, 1])
def test_quote_future_or_stale_timestamp_never_becomes_fresh_read_time(
    monkeypatch, path, delta
):
    h = Harness(monkeypatch)
    h.responses[path]["time"] += delta
    with pytest.raises(ValueError, match="QUOTE_STALE"):
        h.collect()


def test_position_change_and_existing_position_fail_without_any_order_calls(
    monkeypatch,
):
    h = Harness(monkeypatch)
    h.responses["/fapi/v3/positionRisk"] = [
        {"symbol": "BTCUSDT", "positionSide": "BOTH", "positionAmt": "1"}
    ]
    with pytest.raises(ValueError, match="EXISTING_POSITION"):
        h.collect()
    assert h.http_calls == [("GET", "/fapi/v3/positionRisk")]
    h = Harness(monkeypatch)
    counts = [0]

    def changing():
        counts[0] += 1
        return (
            []
            if counts[0] == 1
            else [{"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "1"}]
        )

    h.responses["/fapi/v3/positionRisk"] = changing
    with pytest.raises(ValueError, match="POSITIONS_CHANGED"):
        h.collect()


def test_profile_changes_between_private_reads_and_pg_snapshot_abort(monkeypatch):
    h = Harness(monkeypatch)
    h.change_profile = True
    provider = h.provider()
    with pytest.raises(ValueError, match="POLICY_CHANGED"):
        h.collect(provider)
    assert not h.health_calls and provider.last_verified_account is None


def test_stop_and_deadline_clear_previous_verified_snapshot(monkeypatch):
    h = Harness(monkeypatch)
    provider = h.provider()
    h.collect(provider)
    assert provider.last_verified_account
    h.stop.stopped = True
    assert provider.last_verified_account is None
    with pytest.raises(ValueError, match="STOPPED"):
        h.collect(provider)
    h.stop.stopped = False
    assert provider.last_verified_account is None
    h.on_request = lambda _: h.clock.advance(25001)
    with pytest.raises(ValueError, match="DEADLINE"):
        h.collect(provider)
    assert provider.last_verified_account is None


def test_monotonic_deadline_cannot_be_evaded_by_stationary_wall_clock(monkeypatch):
    h = Harness(monkeypatch)
    h.on_request = lambda _: setattr(h.clock, "elapsed", h.clock.elapsed + 26000)
    with pytest.raises(ValueError, match="DEADLINE"):
        h.collect()


def test_mmr_float_is_conservatively_bounded_and_does_not_depend_on_decimal_context(
    monkeypatch,
):
    h = Harness(monkeypatch)
    with localcontext() as ctx:
        ctx.prec = 3
        ctx.traps[Inexact] = True
        facts = h.collect()
    ratio = Decimal(facts.leverage_proof["venue"]["tiers"][0]["mmr"])
    assert ratio >= Decimal.from_float(0.005) and ratio >= Decimal(".005")
    assert ratio <= Decimal(".005000000000000002")


@pytest.mark.parametrize(
    "defect",
    ["gap", "symbol", "negative_ratio", "bad_leverage", "float_cap", "missing"],
)
def test_tier_facts_are_never_invented(monkeypatch, defect):
    h = Harness(monkeypatch)
    row = h.responses["/fapi/v1/leverageBracket"][0]
    if defect == "symbol":
        row["symbol"] = "ETHUSDT"
    elif defect == "gap":
        row["brackets"][0]["notionalFloor"] = 1
    elif defect == "negative_ratio":
        row["brackets"][0]["maintMarginRatio"] = -0.005
    elif defect == "bad_leverage":
        row["brackets"][0]["initialLeverage"] = True
    elif defect == "float_cap":
        row["brackets"][0]["notionalCap"] = 1000000.1
    else:
        row["brackets"] = []
    with pytest.raises(ValueError):
        h.collect()


def test_real_v3_account_does_not_require_v2_multi_asset_flag(monkeypatch):
    h = Harness(monkeypatch)
    assert "marginAvailable" not in h.responses["/fapi/v3/account"]["assets"][0]
    assert h.collect().capital_base == "1000"
    h.responses["/fapi/v3/account"]["assets"][0]["marginAvailable"] = False
    with pytest.raises(ValueError, match="USDT_COLLATERAL_REQUIRED"):
        h.collect()


def test_exact_spread_is_rounded_up_to_typed_evidence_precision(monkeypatch):
    h = Harness(monkeypatch)
    facts = h.collect()
    book = h.responses["/fapi/v1/ticker/bookTicker"]
    spread = (Fraction(book["askPrice"]) - Fraction(book["bidPrice"])) / Fraction(
        book["bidPrice"]
    )
    recorded = facts.leverage_proof["venue"]["spread_fraction"]
    assert spread <= Fraction(recorded) < spread + Fraction(1, 10**18)
    assert Decimal(recorded).as_tuple().exponent >= -18
    assert run_size((h.decision, h.binding, h.policy, facts)).status == "SIZED"


def test_readonly_transaction_is_required_before_any_account_io(monkeypatch):
    h = Harness(monkeypatch)
    monkeypatch.setattr(MemoryConnection, "autocommit", True)
    with pytest.raises(ValueError, match="TRANSACTION_REQUIRED"):
        h.collect()
    assert not h.http_calls and not h.sql


def test_zero_available_is_real_no_budget_not_missing_data(monkeypatch):
    h = Harness(monkeypatch)
    account = h.responses["/fapi/v3/account"]
    account["availableBalance"] = account["assets"][0]["availableBalance"] = "0"
    facts = h.collect()
    assert facts.available_margin == "0"
    assert run_size((h.decision, h.binding, h.policy, facts)).status == "NO_BUDGET"
    del account["assets"][0]["availableBalance"]
    with pytest.raises(ValueError):
        h.collect()


def test_reservation_row_bound_is_not_silently_truncated(monkeypatch):
    h = Harness(monkeypatch)
    h.held = [("1", "3", None, None, "proof", asdict(h.binding.scope))] * 5001
    provider = h.provider()
    with pytest.raises(ValueError, match="RESERVATION_BOUND"):
        h.collect(provider)
    assert provider.last_verified_account is None


def test_stop_during_read_returns_no_facts_and_no_reusable_snapshot(monkeypatch):
    h = Harness(monkeypatch)
    provider = h.provider()
    h.on_request = lambda _: setattr(h.stop, "stopped", True)
    with pytest.raises(ValueError, match="STOPPED"):
        h.collect(provider)
    assert h.http_calls == [("GET", "/fapi/v3/positionRisk")]
    assert provider.last_verified_account is None


def test_expired_signal_cannot_trigger_new_reads(monkeypatch):
    h = Harness(monkeypatch)
    h.clock.value = h.state.snapshot["expires_at_ms"]
    with pytest.raises(ValueError, match="DEADLINE"):
        h.collect()
    assert not h.http_calls and not h.sql


def test_verified_capital_projection_expiry_is_exclusive(monkeypatch):
    h = Harness(monkeypatch)
    provider = h.provider()
    h.collect(provider)
    deadline = provider.last_verified_account.valid_until_ms
    h.clock.value = deadline - 1
    assert provider.last_verified_account is not None
    h.clock.value = deadline
    assert provider.last_verified_account is None


def test_repeated_io_does_not_refresh_old_catalog_timestamp(monkeypatch):
    h = Harness(monkeypatch)
    h.catalog["observed_at_ms"] -= h.policy.max_fact_age_ms
    h.on_request = lambda _: h.clock.advance(1)
    with pytest.raises(ValueError, match="COLLECTION_STALE"):
        h.collect()
    assert ("GET", "/fapi/v1/exchangeInfo") not in h.http_calls


@pytest.mark.parametrize("defect", ["scope", "signal_environment", "frame_symbol"])
def test_typed_scope_binding_is_checked_before_io(monkeypatch, defect):
    h = Harness(monkeypatch)
    if defect == "scope":
        h.binding = replace(
            h.binding, scope=replace(h.binding.scope, account_id="other")
        )
    elif defect == "signal_environment":
        h.state = replace(h.state, environment="LIVE")
    else:
        h.frame = replace(h.frame, symbol="ETHUSDT")
    provider = h.provider(scope=replace(h.binding.scope, account_id="qa-typed-sizing"))
    with pytest.raises(ValueError, match="INPUT_SCOPE_MISMATCH"):
        h.collect(provider)
    assert not h.http_calls and not h.sql
