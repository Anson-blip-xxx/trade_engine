"""Public research checkpoints; all database writes use disposable QA schemas."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.v2_mover_flow_store import (
    MAX_PAYLOAD_BYTES,
    SOURCE,
    VERSION,
    FlowEvidenceStore,
    _record,
)
from tests.support.v2_admission import database as database_fixture
from v2_core.evidence import canonical, digest

database = database_fixture
DAY = "2026-10-25"
SYMBOL = "BTCUSDT"
ROOT = Path(__file__).resolve().parents[2]


def payload(day=DAY, symbol=SYMBOL, revision=1):
    start = (
        int(
            datetime.combine(
                date.fromisoformat(day), time(), timezone(timedelta(hours=8))
            ).timestamp()
        )
        * 1000
    )
    envelope = {
        "status": "UNKNOWN",
        "fetched_at_ms": start + 86_400_000 + revision,
        "reason": "SOURCE_UNAVAILABLE",
        "rows": [],
    }
    return {
        "version": VERSION,
        "source": SOURCE,
        "symbol": symbol,
        "start_ms": start,
        "end_ms": start + 86_400_000,
        "mapping": {"status": "UNKNOWN", "reason": "SOURCE_UNAVAILABLE"},
        "spot": {**deepcopy(envelope), "source": "BINANCE_SPOT_LIVE"},
        "open_interest": {**deepcopy(envelope), "source": "BINANCE_USDM_LIVE"},
        "funding": {**deepcopy(envelope), "source": "BINANCE_USDM_LIVE"},
    }


@pytest.fixture
def store(database):
    ddl = (ROOT / "db/migrations/20261028_mover_flow_evidence.sql").read_text()
    with database() as conn:
        conn.execute(ddl)
        conn.execute(ddl)  # Safe migration replay.
    return FlowEvidenceStore(database)


def no_database():
    pytest.fail("invalid input must not open a database connection")


@pytest.mark.parametrize(
    "day,symbol",
    [
        (None, SYMBOL),
        (date(2026, 10, 25), SYMBOL),
        ("20261025", SYMBOL),
        ("2026-02-30", SYMBOL),
        ("2026-10-25;DELETE", SYMBOL),
        ("1969-10-25", SYMBOL),
        (DAY, None),
        (DAY, "USDT"),
        (DAY, "BTCUSDC"),
        (DAY, "BTCUSDT;DELETE"),
        (DAY, "BTC USDT"),
        (DAY, "X" * 41 + "USDT"),
    ],
)
def test_invalid_key_never_opens_connection(day, symbol):
    with pytest.raises(ValueError, match="FLOW_EVIDENCE_"):
        FlowEvidenceStore(no_database).read(day, symbol)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", "unrecognized-version"),
        ("source", "BINANCE_TESTNET"),
        ("source", "BINANCE_SPOT_LIVE"),
        ("source", "BINANCE_USDM_LIVE"),
        ("symbol", "ETHUSDT"),
        ("start_ms", 1),
        ("end_ms", True),
        ("mapping", None),
        ("mapping", {"status": "OBSERVED"}),
        ("spot", None),
    ],
)
def test_invalid_identity_or_shape_never_opens_connection(field, value):
    supplied = payload()
    supplied[field] = value
    with pytest.raises(ValueError, match="FLOW_EVIDENCE_"):
        FlowEvidenceStore(no_database).save(DAY, SYMBOL, supplied, complete=False)


@pytest.mark.parametrize(
    "component,maximum", [("spot", 24), ("open_interest", 30), ("funding", 200)]
)
def test_raw_arrays_are_bounded(component, maximum):
    supplied = payload()
    supplied[component]["rows"] = [{}] * (maximum + 1)
    with pytest.raises(ValueError, match="FLOW_EVIDENCE_ENVELOPE"):
        FlowEvidenceStore(no_database).save(DAY, SYMBOL, supplied, complete=False)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "BINANCE_TESTNET"),
        ("source", "BINANCE_SPOT_LIVE"),
        ("source", SOURCE),
        ("status", "COMPLETE"),
        ("fetched_at_ms", True),
        ("fetched_at_ms", -1),
        ("fetched_at_ms", 2**53),
        ("rows", {}),
    ],
)
def test_envelope_source_and_timestamp_are_validated(field, value):
    supplied = payload()
    supplied["funding"][field] = value
    with pytest.raises(ValueError, match="FLOW_EVIDENCE_ENVELOPE"):
        FlowEvidenceStore(no_database).save(DAY, SYMBOL, supplied, complete=False)


@pytest.mark.parametrize("complete", [None, 0, 1, "true"])
def test_complete_must_be_boolean(complete):
    with pytest.raises(ValueError, match="FLOW_EVIDENCE_COMPLETE"):
        FlowEvidenceStore(no_database).save(DAY, SYMBOL, payload(), complete=complete)


def test_payload_size_and_secret_or_float_rejection_precede_connection():
    oversized = payload()
    oversized["spot"]["reason"] = "x" * MAX_PAYLOAD_BYTES
    with pytest.raises(ValueError, match="FLOW_EVIDENCE_SIZE"):
        FlowEvidenceStore(no_database).save(DAY, SYMBOL, oversized, complete=False)
    for name, value in [("api_key", "never-store"), ("value", 1.5)]:
        supplied = payload()
        supplied["funding"][name] = value
        with pytest.raises(ValueError):
            FlowEvidenceStore(no_database).save(DAY, SYMBOL, supplied, complete=False)


@pytest.mark.parametrize("limit", [None, True, 0, -1, 501, "500"])
def test_invalid_retention_limit_never_opens_connection(limit):
    with pytest.raises(ValueError, match="FLOW_EVIDENCE_PRUNE_LIMIT"):
        FlowEvidenceStore(no_database).prune_raw(DAY, limit=limit)


def test_invalid_retention_date_never_opens_connection():
    with pytest.raises(ValueError, match="FLOW_EVIDENCE_DAY"):
        FlowEvidenceStore(no_database).prune_raw("2026-10-25' OR TRUE")


@pytest.mark.parametrize("column,value", [(1, 4), (2, 1), (3, -1), (4, "f" * 64)])
def test_read_rejects_corrupt_metadata_or_digest(column, value):
    supplied = payload()
    row = [supplied, 1, False, 1000, digest(canonical(supplied))]
    row[column] = value
    with pytest.raises(ValueError, match="FLOW_EVIDENCE_CORRUPT"):
        _record(DAY, SYMBOL, row)


def test_migration_and_grants_only_name_the_new_research_table():
    ddl = (ROOT / "db/migrations/20261028_mover_flow_evidence.sql").read_text()
    grants = (ROOT / "deploy/v2-testnet/mover-flow-grants.sql").read_text()
    assert "REFERENCES" not in ddl.upper()
    assert ddl.upper().count("CREATE TABLE") == 1
    assert "v2_mover_flow_evidence" in ddl
    assert "GRANT SELECT,INSERT,UPDATE,DELETE" in grants
    assert "ON trade_v2.v2_mover_flow_evidence TO tradev2movers" in grants
    assert grants.upper().count("GRANT ") == 1
    for forbidden in ("v2_orders", "v2_fills", "v2_trade_intents", "v2_business_state"):
        assert forbidden not in ddl
        assert forbidden not in grants


def test_first_write_round_trips_raw_payload_and_original_observation_times(store):
    assert store.read(DAY, SYMBOL) is None
    supplied = payload()
    supplied["mapping"] = {
        "status": "MATCHED",
        "spot_symbol": SYMBOL,
        "base_asset": "BTC",
        "quote_asset": "USDT",
    }
    supplied["spot"].update(status="OBSERVED", rows=[[123, "1.00", "2.00"]])
    supplied["open_interest"].update(
        status="OBSERVED", rows=[{"timestamp": 123, "sumOpenInterest": "1.20"}]
    )
    supplied["funding"].update(
        status="OBSERVED", rows=[{"fundingTime": 123, "fundingRate": "-0.0001"}]
    )
    original = deepcopy(supplied)
    saved = store.save(DAY, SYMBOL, supplied, complete=False)
    assert supplied == original
    assert saved["payload"] == supplied
    assert saved["input_digest"] == digest(canonical(supplied))
    assert saved["attempts"] == 1
    assert saved["complete"] is False
    assert saved["updated_at_ms"] > 0
    assert store.read(DAY, SYMBOL) == saved


def test_unknown_can_retry_three_times_and_last_persisted_payload_is_returned(store):
    for revision in range(1, 4):
        saved = store.save(DAY, SYMBOL, payload(revision=revision), complete=False)
        assert saved["attempts"] == revision
    assert saved["payload"] == payload(revision=3)
    assert store.save(DAY, SYMBOL, payload(revision=4), complete=True) == saved
    assert store.read(DAY, SYMBOL) == saved


@pytest.mark.parametrize("terminal_absence", [False, True])
def test_complete_checkpoint_cannot_be_overwritten(store, terminal_absence):
    supplied = payload()
    if terminal_absence:
        supplied["mapping"]["reason"] = "NO_EXACT_SPOT_MATCH"
    else:
        for component in ("spot", "open_interest", "funding"):
            supplied[component]["status"] = "OBSERVED"
    complete = store.save(DAY, SYMBOL, supplied, complete=True)
    assert store.save(DAY, SYMBOL, payload(revision=2), complete=False) == complete
    assert store.read(DAY, SYMBOL) == complete
    assert complete["attempts"] == 1


def test_concurrent_writers_share_three_attempt_ceiling(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda revision: store.save(
                    DAY, SYMBOL, payload(revision=revision), complete=False
                ),
                range(1, 9),
            )
        )
    assert sorted(row["attempts"] for row in results) == [1, 2, 3, 3, 3, 3, 3, 3]
    last = store.read(DAY, SYMBOL)
    assert last["attempts"] == 3
    assert all(row == last for row in results if row["attempts"] == 3)


def test_concurrent_completion_preserves_first_completed_payload(store):
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(
            pool.map(
                lambda revision: store.save(
                    DAY, SYMBOL, payload(revision=revision), complete=True
                ),
                range(1, 9),
            )
        )
    assert all(row == results[0] for row in results)
    assert results[0]["attempts"] == 1


@pytest.mark.parametrize("tamper_digest", [False, True])
def test_tampered_checkpoint_is_not_read_or_overwritten(store, database, tamper_digest):
    supplied = payload()
    store.save(DAY, SYMBOL, supplied, complete=False)
    with database() as conn:
        if tamper_digest:
            conn.execute(
                "UPDATE v2_mover_flow_evidence SET input_digest=%s", ("f" * 64,)
            )
        else:
            conn.execute(
                "UPDATE v2_mover_flow_evidence SET payload=%s",
                (Jsonb(payload(revision=2)),),
            )
    with pytest.raises(ValueError, match="FLOW_EVIDENCE_CORRUPT"):
        store.read(DAY, SYMBOL)
    with pytest.raises(ValueError, match="FLOW_EVIDENCE_CORRUPT"):
        store.save(DAY, SYMBOL, payload(revision=3), complete=False)
    with database() as conn:
        assert conn.execute(
            "SELECT attempts FROM v2_mover_flow_evidence"
        ).fetchone() == (1,)


def test_database_constraints_bound_payload_attempts_and_complete(store, database):
    supplied = payload()
    store.save(DAY, SYMBOL, supplied, complete=False)
    for attempts in (0, 4):
        with pytest.raises(psycopg.errors.CheckViolation), database() as conn:
            conn.execute("UPDATE v2_mover_flow_evidence SET attempts=%s", (attempts,))
    with pytest.raises(psycopg.errors.NotNullViolation), database() as conn:
        conn.execute("UPDATE v2_mover_flow_evidence SET complete=NULL")
    oversized = {"raw": "x" * MAX_PAYLOAD_BYTES}
    with pytest.raises(psycopg.errors.CheckViolation), database() as conn:
        conn.execute(
            "UPDATE v2_mover_flow_evidence SET payload=%s", (Jsonb(oversized),)
        )
    assert store.read(DAY, SYMBOL)["attempts"] == 1


def test_unicode_public_symbols_are_isolated_from_other_symbols_and_days(store):
    unicode_symbol = "币安人生USDT"
    first = store.save(
        DAY, unicode_symbol, payload(symbol=unicode_symbol), complete=False
    )
    second = store.save(DAY, SYMBOL, payload(), complete=False)
    other_day = "2026-10-24"
    third = store.save(other_day, SYMBOL, payload(day=other_day), complete=False)
    assert store.read(DAY, unicode_symbol) == first
    assert store.read(DAY, SYMBOL) == second
    assert store.read(other_day, SYMBOL) == third


def test_retention_cutoff_and_maximum_batch_preserve_recent_checkpoints(
    store, database
):
    old = "2026-09-24"
    cutoff = "2026-09-25"
    with database() as conn, conn.cursor() as cursor:
        rows = []
        for index in range(505):
            symbol = f"OLD{index}USDT"
            supplied = payload(day=old, symbol=symbol)
            rows.append(
                (old, symbol, VERSION, Jsonb(supplied), digest(canonical(supplied)))
            )
        cursor.executemany(
            """INSERT INTO v2_mover_flow_evidence
            (day,symbol,method_version,payload,input_digest,complete)
            VALUES(%s,%s,%s,%s,%s,false)""",
            rows,
        )
    boundary = store.save(cutoff, SYMBOL, payload(day=cutoff), complete=True)
    recent = store.save(DAY, SYMBOL, payload(), complete=True)
    assert store.prune_raw(DAY) == 500
    assert store.prune_raw(DAY, limit=3) == 3
    assert store.prune_raw(DAY) == 2
    assert store.prune_raw(DAY) == 0
    assert store.read(old, "OLD0USDT") is None
    assert store.read(cutoff, SYMBOL) == boundary
    assert store.read(DAY, SYMBOL) == recent


def test_research_writes_leave_all_trading_business_counts_unchanged(store, database):
    tables = (
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
    )

    def counts():
        with database() as conn:
            return tuple(
                conn.execute("SELECT count(*) FROM " + table).fetchone()[0]
                for table in tables
            )

    before = counts()
    store.save(DAY, SYMBOL, payload(), complete=False)
    store.save(DAY, SYMBOL, payload(revision=2), complete=True)
    store.read(DAY, SYMBOL)
    store.prune_raw("2026-12-01")
    assert counts() == before
