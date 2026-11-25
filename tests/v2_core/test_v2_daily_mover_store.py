"""Disposable-PG only: resumable research state never edits trading tables."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg.types.json import Jsonb

from services.v2_daily_mover_store import DailyMoverStore
from tests.support.v2_admission import database as database_fixture
from v2_core.account_risk import AccountScope
from v2_core.daily_movers import VERSION

database = database_fixture
SCOPE = AccountScope("BINANCE", "qa-daily-store", "SANDBOX", "FUTURES")
DAY = "2026-10-25"


@pytest.fixture
def store(database):
    ddl = (
        Path(__file__).resolve().parents[2] / "db/migrations/20261026_daily_movers.sql"
    ).read_text()
    with database() as c:
        c.execute(ddl)
        c.execute(ddl)  # Deployment replay is safe.
    return DailyMoverStore(database)


def payload(revision=1):
    return {
        "schema": "qa-mover-report",
        "day": DAY,
        "revision": revision,
        "execution_authorized": False,
    }


def delivery(database, identity):
    with database() as c:
        return c.execute(
            "SELECT status,attempts,message_id FROM v2_daily_mover_delivery WHERE report_id=%s",
            (identity,),
        ).fetchone()


def test_first_day_catalog_is_frozen_on_retry(store):
    assert store.day(DAY) is None
    first = store.start_day(DAY, {"symbols": ["BTCUSDT"]}, 1000)
    later = store.start_day(DAY, {"symbols": ["ETHUSDT"]}, 2000)
    assert first == later
    assert later["catalog"] == {"symbols": ["BTCUSDT"]}
    assert later["observed_at_ms"] == 1000


def test_symbol_unknown_can_resume_complete_cannot_be_overwritten(store):
    store.start_day(DAY, {}, 1000)
    store.save_symbol(DAY, "BTCUSDT", {"complete": False}, [], error="DATA_MISSING")
    assert store.symbols(DAY)["BTCUSDT"]["attempts"] == 1
    store.save_symbol(DAY, "BTCUSDT", {"complete": False}, [], error="TIMEOUT")
    assert store.symbols(DAY)["BTCUSDT"]["attempts"] == 2
    store.save_symbol(
        DAY, "BTCUSDT", {"complete": True, "return_pct": "10"}, [{"t": 1}]
    )
    complete = store.symbols(DAY)["BTCUSDT"]
    store.save_symbol(DAY, "BTCUSDT", {"complete": False}, [], error="LATE_FAILURE")
    assert store.symbols(DAY)["BTCUSDT"] == complete
    assert complete["attempts"] == 3


def test_first_minute_path_and_completed_ranking_remain_frozen(store):
    store.start_day(DAY, {}, 1000)
    store.save_symbol(DAY, "BTCUSDT", {"complete": True}, [])
    store.save_path(DAY, "BTCUSDT", [{"t": 1, "c": "10"}])
    store.save_path(DAY, "BTCUSDT", [{"t": 2, "c": "20"}])
    assert store.symbols(DAY)["BTCUSDT"]["minute_path"] == [{"t": 1, "c": "10"}]
    store.finish_market(DAY, {"status": "partial"}, complete=False)
    assert store.day(DAY)["status"] == "PARTIAL"
    store.finish_market(DAY, {"status": "final"}, complete=True)
    store.finish_market(DAY, {"status": "late"}, complete=False)
    assert store.day(DAY)["ranking"] == {"status": "final"}
    assert store.day(DAY)["status"] == "COMPLETE"


def test_failed_null_path_does_not_block_later_valid_data(store):
    store.start_day(DAY, {}, 1000)
    store.save_symbol(DAY, "BTCUSDT", {"complete": True}, [])
    with pytest.raises(ValueError):
        store.save_path(DAY, "BTCUSDT", None)
    store.save_path(DAY, "BTCUSDT", [{"t": 1, "c": "10"}])
    assert store.symbols(DAY)["BTCUSDT"]["minute_path"] is not None


def test_reports_are_idempotent_revisable_and_account_scoped(store, database):
    first = store.save_report(SCOPE, DAY, payload())
    assert store.save_report(SCOPE, DAY, payload()) == first
    second = store.save_report(SCOPE, DAY, payload(2))
    assert second != first
    assert store.existing(SCOPE, DAY) == (second, payload(2))
    other_scope = replace(SCOPE, account_id="qa-other-daily-store")
    other = store.save_report(other_scope, DAY, payload())
    assert other != first
    assert store.existing(other_scope, DAY) == (other, payload())
    with database() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_daily_mover_reports").fetchone()[0] == 3
        )
    # prior is an accumulation projection, never a full-report reader.
    # This deliberately non-report fixture retains no compatible fields.
    assert store.prior(SCOPE, "2026-10-26") == [{}]


def test_prior_uses_latest_revision_same_method_only_and_bounded_history(
    store, database
):
    store.save_report(SCOPE, "2026-08-01", {"day": "2026-08-01"})
    store.save_report(SCOPE, DAY, payload())
    store.save_report(SCOPE, DAY, payload(2))
    store.save_report(SCOPE, "2026-10-26", {"day": "2026-10-26"})
    with database() as c:
        c.execute(
            """INSERT INTO v2_daily_mover_reports
          (report_id,day,method_version,scope_key,source_digest,report)
          VALUES (%s,%s,'incompatible-old-method',%s,%s,%s)""",
            (
                uuid4(),
                DAY,
                store.scope_key(SCOPE),
                "f" * 64,
                Jsonb({"day": DAY, "wrong_method": True}),
            ),
        )
    assert store.prior(SCOPE, "2026-10-26") == [{}]


def test_one_worker_claims_delivery_and_sent_never_retries(store, database):
    identity = store.save_report(SCOPE, DAY, payload())
    with ThreadPoolExecutor(max_workers=8) as pool:
        claimed = list(pool.map(store.claim_delivery, [identity] * 8))
    assert claimed.count(True) == 1
    assert delivery(database, identity)[:2] == ("SENDING", 1)
    store.delivered(identity, 123)
    assert delivery(database, identity) == ("SENT", 1, 123)
    assert store.claim_delivery(identity) is False


def test_ambiguous_delivery_does_not_retry_or_mark_success(store, database):
    identity = store.save_report(SCOPE, DAY, payload())
    assert store.claim_delivery(identity)
    store.delivered(identity)
    assert delivery(database, identity) == ("UNKNOWN", 1, None)
    assert not store.claim_delivery(identity)


@pytest.mark.parametrize("message_id", [False, True, 0, -1, "123"])
def test_invalid_message_identity_never_marks_sent(store, database, message_id):
    identity = store.save_report(SCOPE, DAY, payload())
    assert store.claim_delivery(identity)
    try:
        store.delivered(identity, message_id)
    except ValueError:
        pass
    assert delivery(database, identity)[0] != "SENT"


def test_timed_out_inflight_delivery_becomes_unknown_not_resent(store, database):
    identity = store.save_report(SCOPE, DAY, payload())
    assert store.claim_delivery(identity)
    with database() as c:
        c.execute(
            "UPDATE v2_daily_mover_delivery SET attempted_at=clock_timestamp()-interval '6 minutes' WHERE report_id=%s",
            (identity,),
        )
    assert store.claim_delivery(identity) is False
    assert delivery(database, identity) == ("UNKNOWN", 1, None)


def test_new_revision_does_not_send_again_same_account_day(store):
    first = store.save_report(SCOPE, DAY, payload())
    assert store.claim_delivery(first)
    store.delivered(first, 123)
    revised = store.save_report(SCOPE, DAY, payload(2))
    assert not store.claim_delivery(revised)
    other = store.save_report(
        replace(SCOPE, account_id="qa-second-account"), DAY, payload()
    )
    assert store.claim_delivery(other)


def test_concurrent_different_revisions_only_one_daily_send(store):
    first = store.save_report(SCOPE, DAY, payload())
    second = store.save_report(SCOPE, DAY, payload(2))
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed = list(pool.map(store.claim_delivery, [first, second]))
    assert claimed.count(True) == 1


def test_old_paths_pruned_in_bounded_batches_reports_and_recent_paths_remain(
    store, database
):
    old = "2026-08-01"
    store.start_day(old, {}, 1000)
    store.start_day(DAY, {}, 2000)
    with database() as c:
        c.execute(
            """INSERT INTO v2_daily_mover_symbols(day,method_version,symbol,status,minute_path)
          SELECT %s,%s,'ASSET'||n::text||'USDT','UNKNOWN','[]'::jsonb FROM generate_series(1,105) n""",
            (old, VERSION),
        )
    store.save_symbol(DAY, "BTCUSDT", {"complete": True}, [])
    store.save_path(DAY, "BTCUSDT", [{"t": 1}])
    report_id = store.save_report(SCOPE, old, payload())
    assert store.prune_raw("2026-10-26") == 100
    assert store.prune_raw("2026-10-26") == 5
    assert store.prune_raw("2026-10-26") == 0
    assert store.symbols(DAY)["BTCUSDT"]["minute_path"] == [{"t": 1}]
    assert store.existing(SCOPE, old)[0] == report_id


def test_research_writes_leave_every_trading_domain_count_unchanged(store, database):
    tables = [
        "v2_orders",
        "v2_fills",
        "v2_settlements",
        "v2_trade_intents",
        "v2_decision_evidence",
        "v2_strategy_decisions",
        "v2_signal_receipts",
        "v2_episodes",
        "v2_domain_outbox",
        "v2_business_state",
    ]

    def counts():
        with database() as c:
            return tuple(
                c.execute("SELECT count(*) FROM " + t).fetchone()[0] for t in tables
            )

    before = counts()
    store.start_day(DAY, {}, 1000)
    store.save_symbol(DAY, "BTCUSDT", {"complete": True}, [])
    store.save_path(DAY, "BTCUSDT", [{"t": 1}])
    store.finish_market(DAY, {}, complete=True)
    identity = store.save_report(SCOPE, DAY, payload())
    assert store.claim_delivery(identity)
    store.delivered(identity, 123)
    store.prune_raw("2026-10-26")
    assert counts() == before
