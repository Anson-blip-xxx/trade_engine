import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest

from services import v2_actual_exit_research as worker
from services.v2_exit_funnel_audit import audit
from tests.support.v2_admission import database as database_fixture
from v2_core.evidence import canonical, digest

database = database_fixture
ROOT = Path(__file__).resolve().parents[2]
NOW = 100000000


@pytest.fixture
def store(database):
    with database() as conn:
        name = conn.execute("SELECT current_schema()").fetchone()[0]
        conn.execute((ROOT / "db/research_exit_schema.sql").read_text())
        conn.execute((ROOT / "db/actual_exit_research.sql").read_text())
        text = (ROOT / "deploy/v2-testnet/actual-exit-views.sql").read_text()
        for query in re.findall(r"CREATE(?: OR REPLACE)? VIEW[\s\S]*?;", text):
            conn.execute(
                query.replace("trade_v2_research.", name + ".").replace(
                    "trade_v2.", name + "."
                )
            )
    return database


def seed(
    connect,
    *,
    source="s3",
    environment="SANDBOX",
    account="one",
    created_ms=30000000,
    tv_peer=False,
):
    identity, order, evidence = str(uuid4()), str(uuid4()), uuid4().hex
    snapshot = {
        "source": source,
        "decided_at": created_ms,
        "features": {"evaluation": {"sizing": {"stop_price": "98"}}},
    }
    if tv_peer:
        snapshot["features"]["context"] = {
            "signal_fusion": {"peers": [{"source": "tv_bridge"}]}
        }
    with connect() as conn:
        conn.execute(
            "INSERT INTO v2_decision_evidence(evidence_ref,config_digest,strategy_version,config,snapshot,created_at) VALUES(%s,%s,'qa','{}',%s::jsonb,to_timestamp(%s/1000.0))",
            (evidence, "a" * 64, json.dumps(snapshot), created_ms),
        )
        conn.execute(
            """INSERT INTO v2_trade_intents(intent_id,exchange,account_id,environment,product,producer,request_key,payload,payload_digest,evidence_ref,config_digest,strategy_version,created_at)
         VALUES(%s,'BINANCE',%s,%s,'FUTURES','s6',%s,%s::jsonb,%s,%s,%s,'qa',to_timestamp(%s/1000.0))""",
            (
                identity,
                account,
                environment,
                identity,
                json.dumps({"symbol": "BTCUSDT", "side": "BUY"}),
                "b" * 64,
                evidence,
                "a" * 64,
                created_ms,
            ),
        )
        conn.execute(
            "INSERT INTO v2_episodes(episode_id,slot_key) VALUES(%s,%s)",
            (identity, identity),
        )
        conn.execute(
            "INSERT INTO v2_orders(order_id,episode_id,leg,client_order_id,request_key,quantity,status,updated_at) VALUES(%s,%s,'OPEN',%s,%s,2,'FILLED',to_timestamp(%s/1000.0))",
            (order, identity, order, order, created_ms + 1000),
        )
        conn.execute(
            """INSERT INTO v2_fills(fill_key,order_id,quantity,price,fee,fee_currency,occurred_at_ms,payload,created_at)
         VALUES(%s,%s,2,100,.12,'USDT',%s,'{}',to_timestamp(%s/1000.0))""",
            (order, order, created_ms + 1000, created_ms + 1000),
        )
    return identity


class Archive:
    environment = "SANDBOX"

    def read(self, target, start, end):
        return [
            {"t": t, "o": "100", "h": "100.2", "l": "99.8", "c": "100", "v": "100"}
            for t in reversed(range(start, end, 60000))
        ]


def config():
    return worker.configuration("qa-actual", 30000000)


def test_sanitized_views_live_exclusion_account_separation_and_future_cutoff(store):
    a = seed(store, source="s3", account="one")
    b = seed(store, source="tv_bridge", account="two")
    live = seed(store, environment="LIVE")
    quant = seed(store, source="quant")
    worker.register(store, config())
    assert set(worker.discover(store, config(), NOW)) == {a, b}
    left = worker.load_facts(store, a, NOW)
    right = worker.load_facts(store, b, NOW)
    assert left["scope_key"] != right["scope_key"] and {
        left["source"],
        right["source"],
    } == {"s3", "tv_bridge"}
    assert "account_id" not in left and "config" not in left and "payload" not in left
    assert left["stop_price"] == "98" and left["opening_terminal"]
    with pytest.raises(ValueError, match="NOT_YET_KNOWN"):
        worker.load_facts(store, a, 30000000)
    for excluded in (live, quant):
        with pytest.raises(ValueError, match="NOT_VISIBLE"):
            worker.load_facts(store, excluded, NOW)


@pytest.mark.parametrize("source", ["s3", "tv_bridge"])
def test_real_sql_archive_model_persistence_chain_without_order_writes(store, source):
    identity = seed(store, source=source)
    counts = "SELECT (SELECT count(*) FROM v2_orders),(SELECT count(*) FROM v2_fills),(SELECT count(*) FROM v2_domain_outbox)"
    with store() as conn:
        before = conn.execute(counts).fetchone()
    first = worker.run_once(store, Archive(), config(), now_ms=NOW)
    assert first[0]["status"] == "DONE" and first[0]["source"] == source
    assert worker.run_once(store, Archive(), config(), now_ms=NOW + 1) == []
    with store() as conn:
        assert conn.execute(counts).fetchone() == before
        raw = conn.execute(
            "SELECT payload_json FROM actual_exit_jobs WHERE intent_id=%s", (identity,)
        ).fetchone()[0]
    report = json.loads(raw)
    assert (
        report["facts"]["intent_id"] == identity
        and report["opening_fee_usdt"] == "0.12"
    )
    assert report["facts"]["actual_settlement"] is None
    assert report["entry"]["quantity"] == "2" and not report["execution_authorized"]


def test_pending_does_not_starve_new_samples_and_waits_expire(store):
    c = config()
    a = seed(store)
    worker.register(store, c)
    failure = {"status": "ACTUAL_ARCHIVE_INCOMPLETE", "execution_authorized": False}
    assert worker.persist(store, c, a, failure, now_ms=NOW) == "PENDING"
    b = seed(store, created_ms=31000000)
    d = seed(store, created_ms=32000000)
    assert worker.discover(store, c, NOW + 1800001) == [b, a, d]
    assert (
        worker.persist(store, c, a, failure, now_ms=NOW + c["wait_hours"] * 3600000)
        == "UNCOVERED"
    )
    assert a not in worker.discover(store, c, NOW + 100000000)


def test_concurrent_idempotency_and_frozen_config(store):
    c = config()
    worker.register(store, c)
    identity = str(uuid4())
    report = {"status": "ANALYZED", "execution_authorized": False}

    def save(_):
        return worker.persist(store, c, identity, report, now_ms=NOW)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert set(pool.map(save, range(4))) == {"DONE"}
    with store() as conn:
        assert conn.execute("SELECT count(*) FROM actual_exit_jobs").fetchone()[0] == 1
    with pytest.raises(ValueError, match="FROZEN"):
        worker.register(store, {**c, "batch": 4})
    worker.persist(store, c, identity, {**report, "extra": "later"}, now_ms=NOW + 1)
    with store() as conn:
        assert (
            "later"
            not in conn.execute("SELECT payload_json FROM actual_exit_jobs").fetchone()[
                0
            ]
        )


def test_missing_archive_retries_and_preserves_original_orders(store):
    identity = seed(store)
    archive = Archive()
    archive.read = lambda *a: []
    result = worker.run_once(store, archive, config(), now_ms=NOW)
    assert (
        result[0]["status"] == "PENDING"
        and result[0]["reason"] == "ACTUAL_ARCHIVE_INCOMPLETE"
    )
    with store() as conn:
        assert (
            conn.execute(
                "SELECT status FROM v2_orders WHERE episode_id=%s", (identity,)
            ).fetchone()[0]
            == "FILLED"
        )


def test_control_funnel_source_digest_guard():
    archive = Archive()
    start, end = 30000000, 39000000
    rows = archive.read("BTCUSDT", start, end)
    stored = {
        "mode": "FORWARD",
        "report": {
            "source_start_ms": start,
            "source_end_ms": end,
            "symbol": "BTCUSDT",
            "version": "same-entry-exhaustion-v1",
            "execution_authorized": False,
            "source_digest": digest(canonical({"minutes": rows})),
            "pairs": [],
            "input_digest": "known",
        },
    }
    assert audit(archive, stored)["status"] == "ANALYZED"
    stored["report"]["source_digest"] = "bad"
    with pytest.raises(ValueError, match="MISSING_OR_CHANGED"):
        audit(archive, stored)


def test_schema_projections_are_security_barriers_and_do_not_expose_keys(store):
    with store() as conn:
        rows = conn.execute(
            "SELECT relname,reloptions FROM pg_class WHERE relnamespace=current_schema()::regnamespace AND relkind='v' AND relname LIKE 'actual_exit_%'"
        ).fetchall()
    assert len(rows) == 4 and all(
        "security_barrier=true" in options for _, options in rows
    )


def test_registration_does_not_relabel_history(store):
    c = config()
    with pytest.raises(ValueError, match="MUST_BE_FUTURE"):
        worker.register(store, c, first_activation_ms=c["start_ms"] + 1)
    worker.register(store, c, first_activation_ms=c["start_ms"] - 1)
    worker.register(store, c, first_activation_ms=NOW)


def test_s3_tv_peer_is_not_mislabeled_as_direct_tv_entry(store):
    identity = seed(store, source="s3", tv_peer=True)
    facts = worker.load_facts(store, identity, NOW)
    assert facts["source"] == "s3" and facts["tv_peer_present"] is True


def test_single_slot_retry_lane_gets_turns(store):
    c = {**config(), "batch": 1}
    worker.register(store, c)
    older = seed(store)
    newer = seed(store, created_ms=31000000)
    worker.persist(
        store,
        c,
        older,
        {"status": "RETRY", "execution_authorized": False},
        now_ms=60000000,
    )
    assert worker.discover(store, c, 1800000 * 40) == [newer]
    assert worker.discover(store, c, 1800000 * 41) == [older]


def test_budget_and_unclosed_horizon_never_read_archive(store):
    seed(store)
    archive = Archive()
    archive.read = lambda *a: pytest.fail("no archive read before closed horizon")
    result = worker.run_once(store, archive, config(), now_ms=31000000)
    assert result[0]["reason"] == "ACTUAL_HORIZON_NOT_CLOSED"
    clock = iter([0, 211])
    assert (
        worker.run_once(
            store, archive, config(), now_ms=NOW, monotonic=lambda: next(clock)
        )
        == []
    )
