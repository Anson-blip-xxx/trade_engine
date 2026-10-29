"""Orchestration QA: no venue calls; optional PG uses the isolated QA fixture."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from test_v2_intent_admission import database as database_fixture

import services.v2_daily_mover_job as job_module
from services.v2_daily_mover_job import DailyMoverJob
from services.v2_daily_mover_market import MoverMarketError
from services.v2_daily_mover_report import seal_report
from services.v2_daily_mover_store import DailyMoverStore
from v2_core.account_risk import AccountScope
from v2_core.daily_movers import DAY, MINUTE, previous_day_window, summarize_day
from v2_core.evidence import canonical, digest

database = database_fixture
NOW = 1_793_000_000_000
WINDOW = previous_day_window(NOW)
SCOPE = AccountScope("BINANCE", "qa-mover-one", "SANDBOX", "FUTURES")


class Query:
    def __init__(self, row=None):
        self.row = row

    def fetchone(self):
        return self.row


class FakeStore:
    def __init__(self, *, scopes=(SCOPE,)):
        self.days, self.rows, self.reports, self.deliveries = {}, {}, {}, {}
        self.report_scopes = {}
        self.account_scopes = list(scopes)
        self.statements, self.saved_reports = [], []
        self.reading = False
        self.sequence = 0

    @contextmanager
    def connect(self):
        self.reading = True
        try:
            yield self
        finally:
            self.reading = False

    def execute(self, sql, params=()):
        self.statements.append((sql, params))
        if "max(day)" in sql:
            return Query((date.fromisoformat(max(self.days)) if self.days else None,))
        return Query()

    def day(self, day):
        return deepcopy(self.days.get(day))

    def start_day(self, day, catalog, observed_ms):
        self.days.setdefault(
            day,
            {
                "catalog": deepcopy(catalog),
                "observed_at_ms": observed_ms,
                "status": "COLLECTING",
                "ranking": None,
            },
        )
        return self.day(day)

    def symbols(self, day):
        return deepcopy(self.rows.get(day, {}))

    def save_symbol(self, day, symbol, summary, hourly, *, error=None):
        rows = self.rows.setdefault(day, {})
        existing = rows.get(symbol)
        if existing and existing["status"] == "COMPLETE":
            return
        rows[symbol] = {
            "summary": deepcopy(summary),
            "hourly": deepcopy(hourly),
            "status": "COMPLETE" if summary and summary.get("complete") else "UNKNOWN",
            "attempts": (existing or {}).get("attempts", 0) + 1,
            "minute_path": None,
            "path_attempts": 0,
            "error_code": error,
        }

    def save_path(self, day, symbol, path):
        row = self.rows[day][symbol]
        if row["minute_path"] is None or (
            row["minute_path"].get("analysis", {}).get("status") == "UNKNOWN"
            and row["path_attempts"] < 3
        ):
            row["minute_path"] = deepcopy(path)
            row["path_attempts"] += 1

    def finish_market(self, day, ranking, *, complete):
        self.days[day].update(
            ranking=deepcopy(ranking), status="COMPLETE" if complete else "PARTIAL"
        )

    def scopes(self, *_):
        return self.account_scopes

    def existing(self, scope, day):
        return self.reports.get((scope.account_id, day))

    def prior(self, *_):
        return []

    def save_report(self, scope, day, report):
        self.sequence += 1
        identity = f"report-{self.sequence}"
        self.reports[(scope.account_id, day)] = (identity, deepcopy(report))
        self.saved_reports.append(deepcopy(report))
        self.deliveries[identity] = "PENDING"
        self.report_scopes[identity] = (scope.account_id, day)
        return identity

    def claim_delivery(self, identity):
        if self.deliveries[identity] != "PENDING":
            return False
        if any(
            other != identity
            and self.report_scopes[other] == self.report_scopes[identity]
            and status in {"SENDING", "SENT", "UNKNOWN"}
            for other, status in self.deliveries.items()
        ):
            return False
        self.deliveries[identity] = "SENDING"
        return True

    def delivered(self, identity, message_id=None):
        self.deliveries[identity] = "SENT" if message_id else "UNKNOWN"

    def prune_raw(self, *_):
        return 0


def bars(start, end, *, up=True, interval_minutes=60):
    step = interval_minutes * MINUTE
    count = (end - start) // step
    values = []
    for i, t in enumerate(range(start, end, step)):
        opened = Decimal(100) + (Decimal(i) / count if up else -Decimal(i) / count)
        closed = Decimal(100) + (
            Decimal(i + 1) / count if up else -Decimal(i + 1) / count
        )
        values.append(
            {
                "t": t,
                "o": str(opened),
                "h": str(max(opened, closed) + Decimal("0.01")),
                "l": str(min(opened, closed) - Decimal("0.01")),
                "c": str(closed),
                "v": "100",
            }
        )
    return values


class FakeMarket:
    def __init__(self, store, symbols=("AAAUSDT", "BBBUSDT")):
        self.store = store
        self.items = [
            {
                "symbol": s,
                "quoteAsset": "USDT",
                "contractType": "PERPETUAL",
                "onboardDate": 0,
                "deliveryDate": 4_133_404_800_000,
                "status": "TRADING",
            }
            for s in symbols
        ]
        self.requests = 0
        self.called = []
        self.errors = {}

    def _request(self, kind, symbol=None):
        assert not self.store.reading, (
            "market request must not hold account read transaction"
        )
        self.called.append((kind, symbol))
        self.requests += 1
        error = self.errors.get((kind, symbol))
        if error:
            raise MoverMarketError(error)

    def catalog(self):
        self._request("catalog")
        return deepcopy(self.items)

    def summary(self, symbol, *, start_ms, end_ms, as_of_ms):
        self._request("summary", symbol)
        data = bars(start_ms, end_ms, up=symbol != "BBBUSDT")
        return summarize_day(
            data,
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            as_of_ms=as_of_ms,
            interval_minutes=60,
        ), data

    def bars(self, symbol, start_ms, end_ms, *, interval_minutes=1):
        self._request("bars", symbol)
        return bars(
            start_ms, end_ms, up=symbol != "BBBUSDT", interval_minutes=interval_minutes
        )


def evidence_loader(conn, scope, *, start_ms, end_ms, symbols, as_of_ms, **_):
    assert conn.reading
    assert any("READ ONLY" in sql for sql, _ in conn.statements)
    return {
        "account_scope": asdict(scope),
        "window_start_ms": start_ms,
        "window_end_ms": end_ms,
        "as_of_ms": as_of_ms,
        "symbols": {
            s: {
                "signal_count": 0,
                "stage_counts": {},
                "trades": [],
                "coverage": {
                    "signals": "BOUNDED_QUERY_COMPLETE",
                    "intents": "BOUNDED_QUERY_COMPLETE",
                    "retention_completeness": "NOT_PROVEN",
                },
            }
            for s in symbols
        },
    }


def job(*, scopes=(SCOPE,), notifier=None, evidence=evidence_loader):
    store = FakeStore(scopes=scopes)
    market = FakeMarket(store)
    return (
        DailyMoverJob(
            store,
            market,
            clock_ms=lambda: NOW,
            evidence_loader=evidence,
            notifier=notifier,
        ),
        store,
        market,
    )


def test_full_collection_reports_and_checkpoints_all_without_trading_or_network():
    worker, store, market = job()
    result = worker.run_day(WINDOW)
    assert result["winners"] == result["losers"] == 1
    assert result["orders_submitted"] is False
    assert store.days[WINDOW["date"]]["status"] == "COMPLETE"
    assert len(store.saved_reports) == 1
    assert len(store.rows[WINDOW["date"]]) == 2
    assert all(row["minute_path"] for row in store.rows[WINDOW["date"]].values())
    assert market.requests == 5


@pytest.mark.parametrize(
    "error",
    [
        "MOVER_RATE_LIMITED",
        "MOVER_SHARED_QUOTA_WAIT",
        "MOVER_JOB_DEADLINE",
        "MOVER_PUBLIC_TRANSPORT_FAILURE",
    ],
)
def test_shared_failure_stops_without_losing_completed_symbol_checkpoint(error):
    worker, store, market = job()
    market.errors[("summary", "BBBUSDT")] = error
    with pytest.raises(MoverMarketError, match=error):
        worker.collect(WINDOW)
    assert store.rows[WINDOW["date"]]["AAAUSDT"]["status"] == "COMPLETE"
    assert not store.saved_reports
    del market.errors[("summary", "BBBUSDT")]
    ranking, _ = worker.collect(WINDOW)
    assert ranking["coverage"]["eligible"] == 2
    assert market.called.count(("summary", "AAAUSDT")) == 1
    assert market.called.count(("catalog", None)) == 1


def test_single_symbol_hourly_failure_becomes_coverage_gap_not_whole_day_failure():
    worker, store, market = job()
    market.errors[("summary", "BBBUSDT")] = "MOVER_KLINES_INVALID"
    result = worker.run_day(WINDOW)
    assert result["coverage"]["eligible"] == 1
    assert result["coverage"]["excluded"] == 1
    assert len(store.saved_reports) == 1
    assert store.days[WINDOW["date"]]["status"] == "PARTIAL"


def test_symbol_error_attempts_are_bounded_across_resumptions():
    worker, _store, market = job()
    market.errors[("summary", "BBBUSDT")] = "MOVER_KLINES_INVALID"
    for _ in range(4):
        worker.collect(WINDOW)
    assert market.called.count(("summary", "BBBUSDT")) == 3


def test_single_selected_minute_path_failure_keeps_other_top_rows_and_report():
    worker, store, market = job()
    market.errors[("bars", "BBBUSDT")] = "MOVER_KLINES_INVALID"
    result = worker.run_day(WINDOW)
    assert result["winners"] == result["losers"] == 1
    assert len(store.saved_reports[0]["rows"]) == 2
    assert store.saved_reports[0]["rows"][1]["path"]["status"] == "UNKNOWN"


def test_complete_day_restart_does_not_repeat_public_requests_or_reports():
    worker, store, market = job()
    worker.run_day(WINDOW)
    count = market.requests
    result = worker.run_day(WINDOW)
    assert market.requests == count
    assert len(store.saved_reports) == 1
    assert result["accounts"][0]["status"] == "EXISTING"


def test_completed_market_without_account_report_repairs_missing_report():
    worker, store, market = job()
    worker.collect(WINDOW)
    assert not store.saved_reports
    count = market.requests
    worker.run_day(WINDOW)
    assert market.requests == count
    assert len(store.saved_reports) == 1


def test_report_is_resealed_after_job_adds_underlying_account_evidence():
    worker, store, _ = job()
    worker.run_day(WINDOW)
    report = store.saved_reports[0]
    assert report == seal_report(report)


def test_shared_quota_mid_minute_collection_retains_prior_paths_for_resume():
    worker, store, market = job()
    market.errors[("bars", "BBBUSDT")] = "MOVER_SHARED_QUOTA_WAIT"
    with pytest.raises(MoverMarketError, match="SHARED_QUOTA_WAIT"):
        worker.collect(WINDOW)
    assert store.rows[WINDOW["date"]]["AAAUSDT"]["minute_path"]
    del market.errors[("bars", "BBBUSDT")]
    worker.run_day(WINDOW)
    assert market.called.count(("summary", "AAAUSDT")) == 1
    assert market.called.count(("summary", "BBBUSDT")) == 1
    assert market.called.count(("bars", "AAAUSDT")) == 1
    assert len(store.saved_reports) == 1


def test_empty_ranking_is_not_sent_as_proven_no_opportunity():
    def reject_empty_loader(_conn, _scope, *, symbols, **_):
        assert symbols, (
            "empty ranking must not call evidence loader with invalid empty symbol set"
        )

    worker, store, market = job(evidence=reject_empty_loader)
    for symbol in ("AAAUSDT", "BBBUSDT"):
        market.errors[("summary", symbol)] = "MOVER_KLINES_INVALID"
    result = worker.run_day(WINDOW)
    assert result["coverage"]["eligible"] == 0
    assert result["coverage"]["excluded"] == 2
    assert all(report["ranking_status"] != "COMPLETE" for report in store.saved_reports)


def test_bad_account_evidence_does_not_block_the_other_account():
    other = replace(SCOPE, account_id="qa-mover-two")

    def loader(conn, scope, **kwargs):
        if scope == SCOPE:
            raise RuntimeError("private-error-must-not-appear")
        return evidence_loader(conn, scope, **kwargs)

    worker, store, _ = job(scopes=(SCOPE, other), evidence=loader)
    result = worker.run_day(WINDOW)
    assert len(result["accounts"]) == 2
    assert any(
        report["scope_key"] == DailyMoverStore.scope_key(other)
        for report in store.saved_reports
    )
    assert "private-error-must-not-appear" not in str(result)
    assert "private-error-must-not-appear" not in str(store.saved_reports)


def test_not_configured_delivery_stays_pending():
    worker, store, _ = job()
    worker.run_day(WINDOW)
    assert list(store.deliveries.values()) == ["PENDING"]


def test_successful_dm_has_one_receipt_and_is_not_resent():
    messages = []

    def notify(message):
        messages.append(message)
        return 123

    worker, store, _ = job(notifier=notify)
    worker.run_day(WINDOW)
    worker.run_day(WINDOW)
    assert len(messages) == 1
    assert list(store.deliveries.values()) == ["SENT"]
    assert "AAAUSDT" in messages[0] and "BBBUSDT" in messages[0]


def test_delivery_timeout_is_unknown_and_never_blindly_resent():
    calls = []

    def notify(_):
        calls.append(1)
        raise TimeoutError("might-already-be-delivered")

    worker, store, _ = job(notifier=notify)
    result = worker.run_day(WINDOW)
    assert "UNKNOWN" in result["accounts"][0]["delivery"]
    worker.run_day(WINDOW)
    assert len(calls) == 1
    assert list(store.deliveries.values()) == ["UNKNOWN"]


@pytest.mark.parametrize("receipt", [None, False, True, 0, -1, "123"])
def test_invalid_delivery_receipt_is_not_claimed_as_sent(receipt):
    worker, store, _ = job(notifier=lambda _: receipt)
    result = worker.run_day(WINDOW)
    assert result["accounts"][0]["delivery"] != "SENT"
    assert list(store.deliveries.values()) == ["UNKNOWN"]


def test_normal_catchup_is_bounded_to_three_local_days():
    worker, store, _ = job()
    store.days[date.fromisoformat(WINDOW["date"]).replace(year=2025).isoformat()] = {}
    windows = []
    worker.run_day = lambda window: windows.append(window) or window
    worker.run()
    assert len(windows) == 3
    assert windows[-1] == WINDOW
    assert all(
        w["end_ms"] - w["start_ms"] == DAY and (w["start_ms"] + 28_800_000) % DAY == 0
        for w in windows
    )


def test_future_manual_day_rejected_before_collection():
    worker, _, market = job()
    future = date.fromisoformat(WINDOW["date"]).fromordinal(
        date.fromisoformat(WINDOW["date"]).toordinal() + 1
    )
    with pytest.raises(ValueError, match="COMPLETED_DAYS"):
        worker.run(day=future.isoformat())
    assert market.requests == 0


def test_initial_evidence_is_refreshed_once_at_maturity_not_every_timer_tick():
    worker, store, market = job()
    clock = {"ms": WINDOW["end_ms"] + 3_600_000}
    worker.clock = lambda: clock["ms"]
    worker.run_day(WINDOW)
    initial = store.saved_reports[0]["evidence_snapshot"]
    assert initial["maturity"] == "INITIAL"
    assert initial["retry_after_ms"] == WINDOW["end_ms"] + 6 * 3_600_000
    requests = market.requests
    clock["ms"] += 3_600_000
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
    assert len(store.saved_reports) == 1
    clock["ms"] = initial["retry_after_ms"]
    worker.run_day(WINDOW)
    assert len(store.saved_reports) == 2
    mature = store.saved_reports[1]["evidence_snapshot"]
    assert mature["maturity"] == "MATURE"
    assert mature["retry_after_ms"] is None
    clock["ms"] += 3_600_000
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
    assert market.requests == requests


def test_mature_revision_does_not_send_second_account_daily_digest():
    messages = []
    worker, store, _ = job(notifier=lambda message: messages.append(message) or 123)
    clock = {"ms": WINDOW["end_ms"] + 3_600_000}
    worker.clock = lambda: clock["ms"]
    worker.run_day(WINDOW)
    clock["ms"] = WINDOW["end_ms"] + 6 * 3_600_000
    worker.run_day(WINDOW)
    assert len(store.saved_reports) == 2
    assert len(messages) == 1


def test_explicit_evidence_refresh_creates_revision_without_second_daily_dm_or_market_io():
    messages = []
    notifier = lambda message: messages.append(message) or 123
    worker, store, market = job(notifier=notifier)
    worker.run_day(WINDOW)
    request_count = market.requests
    refreshed = DailyMoverJob(
        store,
        market,
        clock_ms=lambda: NOW + 1,
        evidence_loader=evidence_loader,
        notifier=notifier,
        force_evidence_refresh=True,
    )
    result = refreshed.run_day(WINDOW)
    assert result["accounts"][0]["status"] == "CREATED"
    assert len(store.saved_reports) == 2
    assert len(messages) == 1
    assert market.requests == request_count
    assert (
        store.saved_reports[0]["content_digest"]
        != store.saved_reports[1]["content_digest"]
    )
    assert worker.run_day(WINDOW)["accounts"][0]["status"] == "EXISTING"
    assert len(store.saved_reports) == 2


def test_unavailable_minute_path_retries_are_bounded_and_other_paths_stay_cached():
    worker, store, market = job()
    market.errors[("bars", "BBBUSDT")] = "MOVER_KLINES_INVALID"
    for _ in range(4):
        worker.collect(WINDOW)
    assert market.called.count(("bars", "BBBUSDT")) == 3
    assert market.called.count(("bars", "AAAUSDT")) == 1
    assert store.rows[WINDOW["date"]]["BBBUSDT"]["path_attempts"] == 3


def test_read_failure_retries_at_bounded_hour_then_recovers():
    state = {"fail": True, "calls": 0}

    def loader(conn, scope, **kwargs):
        state["calls"] += 1
        if state["fail"]:
            raise RuntimeError("private-raw-error")
        return evidence_loader(conn, scope, **kwargs)

    worker, store, _ = job(evidence=loader)
    clock = {"ms": NOW}
    worker.clock = lambda: clock["ms"]
    worker.run_day(WINDOW)
    first = store.saved_reports[0]["evidence_snapshot"]
    assert first["status"] == "READ_INCOMPLETE"
    assert first["retry_after_ms"] == NOW + 3_600_000
    clock["ms"] += 3_599_999
    worker.run_day(WINDOW)
    assert state["calls"] == 1
    state["fail"] = False
    clock["ms"] += 1
    worker.run_day(WINDOW)
    assert state["calls"] == 2
    assert store.saved_reports[-1]["evidence_snapshot"]["status"] == "READ_COMPLETED"


def test_partial_symbol_timeout_marks_account_snapshot_retryable():
    def loader(conn, scope, **kwargs):
        evidence = evidence_loader(conn, scope, **kwargs)
        evidence["symbols"]["AAAUSDT"]["signal_count"] = None
        evidence["symbols"]["AAAUSDT"]["coverage"].update(
            signals="UNAVAILABLE", signal_error="QUERY_TIMEOUT"
        )
        return evidence

    worker, store, _ = job(evidence=loader)
    worker.run_day(WINDOW)
    report = store.saved_reports[-1]
    assert report["evidence_snapshot"]["status"] == "READ_INCOMPLETE"
    assert report["evidence_snapshot"]["retry_after_ms"] == NOW + 3_600_000
    assert report["rows"][0]["evidence"]["signal_count"] is None


def test_settlement_query_timeout_is_retryable_not_final_zero_pnl():
    def loader(conn, scope, **kwargs):
        evidence = evidence_loader(conn, scope, **kwargs)
        evidence["symbols"]["AAAUSDT"]["trades"] = [
            {
                "side": "BUY",
                "opening_quantity": "1",
                "closing_quantity": "1",
                "opened_ms": WINDOW["start_ms"] + MINUTE,
                "closed_ms": WINDOW["start_ms"] + 2 * MINUTE,
                "settlement_state": "LABEL_UNAVAILABLE",
                "net_pnl_available": False,
                "net_pnl": None,
                "label_read_error": "QUERY_TIMEOUT",
            }
        ]
        return evidence

    worker, store, _ = job(evidence=loader)
    worker.run_day(WINDOW)
    report = store.saved_reports[-1]
    assert report["evidence_snapshot"]["status"] == "READ_INCOMPLETE"
    assert report["evidence_snapshot"]["retry_after_ms"] == NOW + 3_600_000
    assert report["rows"][0]["evidence"]["trades"][0]["actual_settled_net_usdt"] is None


def test_optional_fill_path_load_failure_preserves_successful_signal_evidence():
    worker, store, _ = job()
    worker.path_reader = lambda *_: []

    def broken(*_, **__):
        raise RuntimeError("optional-fill-read-failed")

    worker.case_loader = broken
    worker.run_day(WINDOW)
    report = store.saved_reports[-1]
    assert report["account_evidence"] is not None
    assert report["rows"][0]["evidence"]["status"] == "OBSERVED_BOUNDED"
    assert report["actual_same_environment_paths"]["status"] == "UNKNOWN"


def test_optional_archive_enrichment_runs_after_read_transaction_closed(monkeypatch):
    worker, store, _ = job()
    calls = []

    def load_cases(conn, scope, evidence, **kwargs):
        assert conn.reading
        assert evidence["account_scope"] == asdict(scope)
        return {"cases": [], "source": "BINANCE_USDM_SANDBOX"}

    def enrich(loaded, reader, **kwargs):
        assert not store.reading
        calls.append(loaded)
        return {"status": "NO_ELIGIBLE_CASES", "source": "BINANCE_USDM_SANDBOX"}

    worker.case_loader = load_cases
    worker.path_reader = lambda *_: []
    monkeypatch.setattr(job_module, "enrich_case_paths", enrich)
    worker.run_day(WINDOW)
    assert len(calls) == 1
    assert (
        store.saved_reports[0]["actual_same_environment_paths"]["source"]
        == "BINANCE_USDM_SANDBOX"
    )


@pytest.fixture
def daily_store(database):
    ddl = (
        Path(__file__).resolve().parents[2] / "db/migrations/20261026_daily_movers.sql"
    )
    with database() as conn:
        conn.execute(ddl.read_text())
    return DailyMoverStore(database)


def test_pg_completed_symbol_is_not_overwritten_by_retry(daily_store):
    store = daily_store
    store.start_day(WINDOW["date"], [], NOW)
    store.save_symbol(
        WINDOW["date"], "AAAUSDT", {"complete": True, "value": "original"}, []
    )
    store.save_symbol(WINDOW["date"], "AAAUSDT", None, None, error="FAILURE")
    row = store.symbols(WINDOW["date"])["AAAUSDT"]
    assert row["summary"] == {"complete": True, "value": "original"}
    assert row["attempts"] == 1


def test_pg_report_idempotency_and_concurrent_delivery_claim(daily_store):
    store = daily_store
    payload = {"version": "qa", "market_digest": "abc", "execution_authorized": False}
    with ThreadPoolExecutor(max_workers=4) as pool:
        identities = list(
            pool.map(
                lambda _: store.save_report(SCOPE, WINDOW["date"], payload), range(4)
            )
        )
    assert len(set(identities)) == 1
    identity = identities[0]
    with ThreadPoolExecutor(max_workers=4) as pool:
        claims = list(pool.map(lambda _: store.claim_delivery(identity), range(4)))
    assert claims.count(True) == 1
    store.delivered(identity, 123)
    assert store.claim_delivery(identity) is False
    with store.connect() as conn:
        assert conn.execute(
            "SELECT status,attempts,message_id FROM v2_daily_mover_delivery WHERE report_id=%s",
            (identity,),
        ).fetchone() == ("SENT", 1, 123)
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0


def test_pg_crash_after_claim_becomes_unknown_not_redelivered(daily_store):
    store = daily_store
    identity = store.save_report(SCOPE, WINDOW["date"], {"research_only": True})
    assert store.claim_delivery(identity)
    with store.connect() as conn:
        conn.execute(
            "UPDATE v2_daily_mover_delivery SET attempted_at=clock_timestamp()-interval '6 minutes' WHERE report_id=%s",
            (identity,),
        )
    assert not store.claim_delivery(identity)
    with store.connect() as conn:
        assert conn.execute(
            "SELECT status,attempts FROM v2_daily_mover_delivery WHERE report_id=%s",
            (identity,),
        ).fetchone() == ("UNKNOWN", 1)


def test_pg_account_separation_and_prior_latest_revision(daily_store):
    store = daily_store
    other = replace(SCOPE, account_id="other-qa-account")
    first = store.save_report(SCOPE, "2026-10-01", {"revision": 1})
    second = store.save_report(SCOPE, "2026-10-01", {"revision": 2})
    third = store.save_report(other, "2026-10-01", {"revision": 3})
    assert len({first, second, third}) == 3
    assert store.existing(SCOPE, "2026-10-01")[1] == {"revision": 2}
    assert store.prior(SCOPE, "2026-10-02") == [{"revision": 2}]
    assert store.scope_key(SCOPE) == digest(canonical(asdict(SCOPE)))


def test_pg_raw_retention_keeps_compact_daily_features_and_reports(daily_store):
    store = daily_store
    store.start_day("2026-08-01", [], NOW)
    store.save_symbol(
        "2026-08-01", "AAAUSDT", {"complete": True}, [{"summary": "keep"}]
    )
    store.save_path("2026-08-01", "AAAUSDT", {"bars": [{"raw": "expire"}]})
    identity = store.save_report(SCOPE, "2026-08-01", {"summary": "keep"})
    assert store.prune_raw("2026-10-01") == 1
    row = store.symbols("2026-08-01")["AAAUSDT"]
    assert row["minute_path"] is None
    assert row["summary"] == {"complete": True}
    assert row["hourly"] == [{"summary": "keep"}]
    assert store.existing(SCOPE, "2026-08-01")[0] == identity
