"""Positioning research only; writes use explicitly disposable Unix-socket QA."""

import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

from services.v2_positioning_store import (
    DAY_MS,
    SLOT_MS,
    SOURCE,
    VERSION,
    PositioningStore,
    _catalog_symbols,
    _prepared,
)
from tests.support.v2_admission import database as database_fixture
from tests.support.v2_admission import intent
from v2_core.evidence import DecisionEvidence, EvidenceStore, canonical
from v2_core.intents import IntentStore

database = database_fixture
ROOT = Path(__file__).resolve().parents[2]
SLOT = (time.time_ns() // 1_000_000 // SLOT_MS - 2) * SLOT_MS
NOW = SLOT + 100_000
FEATURES = {
    "status": "UNKNOWN",
    "execution_authorized": False,
    "automatic_parameter_updates": False,
    "promotion_authorized": False,
}


def bundle(symbol="BTCUSDT", slot=SLOT):
    return {
        "version": VERSION,
        "source": SOURCE,
        "symbol": symbol,
        "started_at_ms": slot + 1,
        "finished_at_ms": slot + 2,
        "components": {
            "open_interest": {
                "status": "UNKNOWN",
                "source": "BINANCE_USDM_LIVE",
                "endpoint": "/futures/data/openInterestHist",
                "fetched_at_ms": slot + 2,
                "rows": [],
                "reason": "SOURCE_UNAVAILABLE",
                "sha256": None,
            }
        },
    }


def catalog(n=30):
    return [
        {
            "symbol": f"COIN{i}USDT",
            "status": "TRADING",
            "contractType": "PERPETUAL",
            "quoteAsset": "USDT",
            "onboardDate": 0,
            "deliveryDate": 2**53 - 1,
        }
        for i in range(n)
    ]


def no_database():
    pytest.fail("invalid research input must not open a database connection")


@pytest.fixture
def store(database):
    with database() as conn:
        for file in (
            "20260925_tenant_registry.sql",
            "20260928_execution_routes.sql",
            "20260928_execution_draining.sql",
            "20261026_daily_movers.sql",
            "20261031_positioning_research.sql",
        ):
            conn.execute((ROOT / "db/migrations" / file).read_text())
        conn.execute(
            (ROOT / "db/migrations/20261031_positioning_research.sql").read_text()
        )
    return PositioningStore(database)


def save_catalog(database, rows=None, ranking=None, observed=NOW - 60_000):
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_daily_mover_days(day,method_version,status,catalog,catalog_observed_at_ms,ranking) VALUES('2026-10-30','daily-movers-utc8-v1','PARTIAL',%s,%s,%s)",
            (
                Jsonb(catalog() if rows is None else rows),
                observed,
                Jsonb({} if ranking is None else ranking),
            ),
        )


def save(store, symbol="BTCUSDT", slot=SLOT, **changes):
    data = bundle(symbol, slot)
    data.update(changes)
    return store.save(
        data, slot_ms=slot, membership=["FIXED_CONTROL"], features=FEATURES
    )


@pytest.mark.parametrize("value", [True, -1, 2**53, "123", 1.5])
def test_bad_clock_never_connects(value):
    with pytest.raises(ValueError, match="POSITIONING_"):
        PositioningStore(no_database).select_symbols(value)


@pytest.mark.parametrize("value", [0, True, -1, 41, "8"])
def test_bad_selection_limit_never_connects(value):
    with pytest.raises(ValueError, match="POSITIONING_"):
        PositioningStore(no_database).select_symbols(NOW, value)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "old"},
        {"source": "BINANCE_TESTNET"},
        {"symbol": "BTCUSDC"},
        {"symbol": "BTCUSDT;DELETE"},
        {"started_at_ms": SLOT - 1},
        {"started_at_ms": SLOT + SLOT_MS},
        {"finished_at_ms": SLOT},
        {"finished_at_ms": SLOT + SLOT_MS * 2 + 1},
        {"components": {}},
        {"account_id": "should-never-be-stored"},
    ],
)
def test_invalid_bundle_never_connects(changes):
    with pytest.raises(ValueError, match="POSITIONING_"):
        save(PositioningStore(no_database), **changes)


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_authorized", True),
        ("automatic_parameter_updates", True),
        ("promotion_authorized", True),
        ("execution_authorized", 0),
    ],
)
def test_features_never_authorize_execution(field, value):
    with pytest.raises(ValueError, match="POSITIONING_RESEARCH_ONLY"):
        PositioningStore(no_database).save(
            bundle(),
            slot_ms=SLOT,
            membership=["FIXED_CONTROL"],
            features={**FEATURES, field: value},
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("api_key", "secret"),
        ("account_id", "private"),
        ("tenant_id", "private"),
        ("number", 1.2),
        ("number", float("nan")),
        ("number", float("inf")),
        ("nested", {"credential_ref": "private"}),
    ],
)
def test_secrets_identity_and_float_json_never_connect(field, value):
    with pytest.raises(ValueError, match="POSITIONING_"):
        PositioningStore(no_database).save(
            bundle(),
            slot_ms=SLOT,
            membership=["FIXED_CONTROL"],
            features={**FEATURES, field: value},
        )


@pytest.mark.parametrize(
    "membership",
    [[], ["OTHER"], ["FIXED_CONTROL", "FIXED_CONTROL"], "FIXED_CONTROL", [None]],
)
def test_bad_membership_rejected(membership):
    with pytest.raises(ValueError, match="POSITIONING_"):
        PositioningStore(no_database).save(
            bundle(), slot_ms=SLOT, membership=membership, features=FEATURES
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("reason", None),
        ("reason", "HTTP secret material"),
        ("rows", [{}]),
        ("fetched_at_ms", SLOT),
        ("sha256", "bad"),
        ("endpoint", "https://evil.example"),
        ("source", "private user"),
    ],
)
def test_invalid_component_rejected(field, value):
    data = bundle()
    data["components"]["open_interest"][field] = value
    with pytest.raises(ValueError, match="POSITIONING_"):
        PositioningStore(no_database).save(
            data, slot_ms=SLOT, membership=["FIXED_CONTROL"], features=FEATURES
        )


def test_json_size_and_depth_are_bounded():
    with pytest.raises(ValueError, match="POSITIONING_JSON_BOUND"):
        _prepared(
            bundle(), SLOT, ["FIXED_CONTROL"], {**FEATURES, "padding": "x" * 262144}
        )
    nested = {}
    for _ in range(20):
        nested = {"nested": nested}
    with pytest.raises(ValueError, match="POSITIONING_JSON_BOUND"):
        _prepared(bundle(), SLOT, ["FIXED_CONTROL"], {**FEATURES, **nested})


@pytest.mark.parametrize(
    "changes",
    [
        {"source_host": "evil.example"},
        {"execution_authorized": True},
        {"promotion_authorized": 0},
        {"halt_reason": "raw token detail"},
    ],
)
def test_public_collector_metadata_is_constrained(changes):
    with pytest.raises(ValueError, match="POSITIONING_"):
        save(PositioningStore(no_database), **changes)


def test_component_digest_must_match_rows():
    data = bundle()
    data["components"]["open_interest"]["sha256"] = "a" * 64
    with pytest.raises(ValueError, match="POSITIONING_COMPONENT_DIGEST_MISMATCH"):
        _prepared(data, SLOT, ["FIXED_CONTROL"], FEATURES)


def test_actual_collector_describer_store_contract_without_network(store):
    from test_v2_positioning_market import Harness

    from v2_core.positioning_evidence import describe

    data = Harness().collect()
    features = describe(
        data, cutoff_ms=data["finished_at_ms"], as_of_ms=data["finished_at_ms"]
    )
    row = store.save(
        data,
        slot_ms=data["started_at_ms"] // SLOT_MS * SLOT_MS,
        membership=["FIXED_CONTROL"],
        features=features,
    )
    assert row["inserted"]
    stored = store.recent("TESTUSDT", before_ms=row["first_received_at_ms"])[0]
    assert stored["features"] == features and stored["bundle"] == data


def test_catalog_exact_trade_eligibility_and_window():
    values = catalog(8)
    values[0]["status"] = "SETTLING"
    values[1]["quoteAsset"] = "USDC"
    values[2]["contractType"] = "CURRENT_QUARTER"
    values[3]["onboardDate"] = NOW + 1
    values[4]["deliveryDate"] = NOW
    values[5]["deliveryDate"] = None
    values[6]["onboardDate"] = True
    assert _catalog_symbols(values, NOW) == {"COIN7USDT"}


def test_no_catalog_does_not_invent_symbols(store):
    assert store.select_symbols(NOW) == []


@pytest.mark.parametrize("observed", [NOW + 1, NOW - 3 * DAY_MS - 1])
def test_future_or_stale_catalog_unavailable(store, database, observed):
    save_catalog(database, observed=observed)
    assert store.select_symbols(NOW) == []


def test_first_slot_wins_and_receipt_is_database_clock(store):
    first = save(store)
    again = save(store)
    changed = bundle()
    changed["components"]["open_interest"]["reason"] = "LATE_RETRY"
    later = store.save(
        changed, slot_ms=SLOT, membership=["FIXED_CONTROL"], features=FEATURES
    )
    assert first["inserted"] and not again["inserted"] and not later["inserted"]
    assert again["same_input"] and not later["same_input"]
    assert first["observation_id"] == again["observation_id"] == later["observation_id"]
    assert first["first_received_at_ms"] == later["first_received_at_ms"] > SLOT + 2
    record = store.recent("BTCUSDT", before_ms=first["first_received_at_ms"])[0]
    assert (
        record["bundle"]["components"]["open_interest"]["reason"]
        == "SOURCE_UNAVAILABLE"
    )


def test_concurrent_slot_commits_one_first_receipt(store):
    with ThreadPoolExecutor(4) as pool:
        result = list(pool.map(lambda _: save(store), range(4)))
    assert sum(row["inserted"] for row in result) == 1
    assert len({row["observation_id"] for row in result}) == 1
    assert len({row["first_received_at_ms"] for row in result}) == 1


def test_recent_rejects_backfilled_evidence_before_receipt(store):
    row = save(store)
    assert store.recent("BTCUSDT", before_ms=SLOT + 1000) == []
    assert store.recent("BTCUSDT", before_ms=row["first_received_at_ms"] - 1) == []
    assert len(store.recent("BTCUSDT", before_ms=row["first_received_at_ms"])) == 1
    assert store.recent("ETHUSDT", before_ms=row["first_received_at_ms"]) == []


def test_observations_and_runs_cannot_be_updated(store, database):
    row = save(store)
    run = store.record_run(
        started_at_ms=SLOT,
        completed_at_ms=SLOT + 1,
        status="PARTIAL",
        coverage={"unknown": 1},
        error_code="PUBLIC_DATA_UNAVAILABLE",
    )
    for table, field, identity in [
        ("v2_positioning_observations", "observation_id", row["observation_id"]),
        ("v2_positioning_runs", "run_id", run),
    ]:
        with (
            pytest.raises(psycopg.errors.RaiseException, match="IMMUTABLE"),
            database() as conn,
        ):
            conn.execute(
                f"UPDATE {table} SET {field}={field} WHERE {field}=%s", (identity,)
            )


def test_selection_fixed_controls_ignore_ranking_and_rotate_oldest(store, database):
    save_catalog(database, catalog(60))
    first = store.select_symbols(NOW, 40)
    assert len(first) == 20 and all(r["membership"] == ["FIXED_CONTROL"] for r in first)
    chosen = store.select_symbols(NOW, 8)
    for row in chosen:
        save(store, row["symbol"])
    second = store.select_symbols(NOW, 8)
    assert not {r["symbol"] for r in chosen} & {r["symbol"] for r in second}
    third = store.select_symbols(NOW + SLOT_MS, 40)
    assert {r["symbol"] for r in first} == {r["symbol"] for r in third}
    assert {r["symbol"] for r in chosen}.isdisjoint({r["symbol"] for r in third[:12]})


def test_top_cohort_not_used_to_replace_fixed_controls(store, database):
    save_catalog(database, catalog(60))
    baseline = {r["symbol"] for r in store.select_symbols(NOW, 40)}
    extra = next(r["symbol"] for r in catalog(60) if r["symbol"] not in baseline)
    with database() as conn:
        conn.execute(
            "UPDATE v2_daily_mover_days SET ranking=%s",
            (Jsonb({"winners": [{"symbol": extra}]}),),
        )
    result = store.select_symbols(NOW, 40)
    assert {
        r["symbol"] for r in result if "FIXED_CONTROL" in r["membership"]
    } == baseline
    assert next(r for r in result if r["symbol"] == extra)["membership"] == [
        "TOP_MOVER_DIAGNOSTIC"
    ]


def seed_position(
    database, symbol, *, environment="SANDBOX", bound=True, filled=True, closed=False
):
    tenant, registry, account = uuid4(), uuid4(), "qa-" + uuid4().hex
    evidence = DecisionEvidence(
        "strategy-v1",
        '{"size":"0.01"}',
        canonical(
            {
                "observed_at": 1,
                "decided_at": 2,
                "source": "test",
                "symbol": symbol,
                "rationale": "qa",
                "features": {},
            }
        ),
    )
    EvidenceStore(database).put(evidence)
    item = replace(
        intent(),
        symbol=symbol,
        account_id=account,
        environment=environment,
        evidence_ref=evidence.evidence_ref,
    )
    IntentStore(database).admit(item)
    with database() as conn:
        conn.execute(
            "INSERT INTO v2_tenants(tenant_id,display_name) VALUES(%s,'QA')", (tenant,)
        )
        conn.execute(
            "INSERT INTO v2_tenant_accounts(registry_id,tenant_id,exchange,account_id,environment,product,display_name,credential_ref,version,status) VALUES(%s,%s,'BINANCE',%s,%s,'FUTURES','QA',%s,1,'ENROLLED_UNVERIFIED')",
            (registry, tenant, account, environment, uuid4()),
        )
        if bound:
            conn.execute(
                "INSERT INTO v2_execution_routes(tenant_id,environment,epoch,target_registry,binding_version,phase,worker_token) VALUES(%s,%s,1,%s,1,'ACTIVE',%s)",
                (tenant, environment, registry, uuid4()),
            )
        conn.execute(
            "INSERT INTO v2_episodes(episode_id,slot_key) VALUES(%s,%s)",
            (item.intent_id, str(uuid4())),
        )
        for leg in ["OPEN", "CLOSE"] if closed else ["OPEN"]:
            order = uuid4()
            conn.execute(
                "INSERT INTO v2_orders(order_id,episode_id,leg,client_order_id,request_key,quantity) VALUES(%s,%s,%s,%s,%s,1)",
                (order, item.intent_id, leg, str(uuid4()), str(uuid4())),
            )
            if filled:
                conn.execute(
                    "INSERT INTO v2_fills(fill_key,order_id,quantity,price,fee,fee_currency,occurred_at_ms,payload) VALUES(%s,%s,1,1,0,'USDT',%s,'{}')",
                    (str(uuid4()), order, SLOT),
                )


def test_active_lane_scoped_to_filled_current_sandbox_and_research_not_starved(
    store, database
):
    save_catalog(database, catalog(70))
    controls = {r["symbol"] for r in store.select_symbols(NOW, 40)}
    outside = [r["symbol"] for r in catalog(70) if r["symbol"] not in controls]
    for symbol in outside[:6]:
        seed_position(database, symbol)
    seed_position(database, outside[6], environment="LIVE")
    seed_position(database, outside[7], bound=False)
    seed_position(database, outside[8], filled=False)
    seed_position(database, outside[9], closed=True)
    result = store.select_symbols(NOW, 8)
    assert len(result) == 8
    assert sum("ACTIVE_SANDBOX_POSITION" in r["membership"] for r in result) == 4
    assert sum("FIXED_CONTROL" in r["membership"] for r in result) == 4
    assert not {r["symbol"] for r in result} & set(outside[6:10])
    assert all(set(r) == {"symbol", "membership"} for r in result)


def test_run_records_safe_status_and_coverage_and_never_orders(store, database):
    identity = store.record_run(
        started_at_ms=SLOT,
        completed_at_ms=SLOT + 1,
        status="NO_SYMBOLS",
        coverage={"eligible": 0},
    )
    rows = store.recent_runs(before_ms=time.time_ns() // 1_000_000 + 100)
    assert rows[0]["run_id"] == identity and rows[0]["coverage"] == {"eligible": 0}
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM v2_domain_outbox").fetchone()[0] == 0


def test_minimal_research_role_cannot_read_credentials_or_write_orders(store, database):
    from psycopg import sql

    role = "codex_positioning_qa_" + uuid4().hex
    with database() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        conn.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(role)
            )
        )
        for table, columns in {
            "v2_daily_mover_days": "catalog,ranking,catalog_observed_at_ms,day,method_version",
            "v2_execution_routes": "tenant_id,target_registry,binding_version,environment,phase",
            "v2_tenant_accounts": "tenant_id,registry_id,version,exchange,account_id,environment,product",
            "v2_trade_intents": "intent_id,payload,exchange,account_id,environment,product",
            "v2_episodes": "episode_id,status",
            "v2_orders": "order_id,episode_id,leg",
            "v2_fills": "order_id,quantity",
        }.items():
            conn.execute(
                sql.SQL("GRANT SELECT ({}) ON {} TO {}").format(
                    sql.SQL(",").join(map(sql.Identifier, columns.split(","))),
                    sql.Identifier(table),
                    sql.Identifier(role),
                )
            )
        conn.execute(
            sql.SQL(
                "GRANT SELECT,INSERT,DELETE ON v2_positioning_observations,v2_positioning_runs TO {}"
            ).format(sql.Identifier(role))
        )

    @contextmanager
    def restricted():
        with database() as conn:
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            yield conn

    try:
        save_catalog(database)
        research = PositioningStore(restricted)
        assert len(research.select_symbols(NOW)) == 8
        result = save(research)
        assert research.recent("BTCUSDT", before_ms=result["first_received_at_ms"])
        research.record_run(
            started_at_ms=SLOT, completed_at_ms=SLOT, status="COMPLETE", coverage={}
        )
        seed_old_observations(database, count=3)
        assert research.prune(NOW, limit=2) == 2
        assert research.prune(NOW, limit=2) == 1
        assert research.prune(NOW, limit=2) == 0
        assert research.recent("BTCUSDT", before_ms=result["first_received_at_ms"])
        for query in (
            "SELECT credential_ref FROM v2_tenant_accounts",
            "SELECT blockers FROM v2_execution_routes",
            "DELETE FROM v2_orders",
            "DELETE FROM v2_trade_intents",
            "UPDATE v2_positioning_observations SET source=source",
        ):
            with (
                pytest.raises(psycopg.errors.InsufficientPrivilege),
                restricted() as conn,
            ):
                conn.execute(query)
    finally:
        with database() as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "raw error"},
        {"error_code": "secret response text"},
        {"coverage": {"api_key": "forbidden"}},
        {"coverage": {"ratio": 0.5}},
        {"completed_at_ms": SLOT - 1},
    ],
)
def test_invalid_run_cannot_connect(changes):
    options = {
        "started_at_ms": SLOT,
        "completed_at_ms": SLOT + 1,
        "status": "FAILED",
        "coverage": {},
    }
    options.update(changes)
    with pytest.raises(ValueError, match="POSITIONING_"):
        PositioningStore(no_database).record_run(**options)


def seed_old_observations(database, count=3):
    old = SLOT - 40 * DAY_MS
    for number in range(count):
        raw = bundle(f"OLD{number}USDT", old)
        _, membership, fingerprint = _prepared(raw, old, ["FIXED_CONTROL"], FEATURES)
        with database() as conn:
            conn.execute(
                "INSERT INTO v2_positioning_observations(observation_id,source,symbol,slot_ms,method_version,bundle,features,membership,input_digest,first_received_at_ms) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    uuid4(),
                    SOURCE,
                    raw["symbol"],
                    old,
                    VERSION,
                    Jsonb(raw),
                    Jsonb(FEATURES),
                    Jsonb(membership),
                    fingerprint,
                    old + 100,
                ),
            )


def test_bounded_prune_keeps_recent_and_trading_data(store, database):
    seed_old_observations(database)
    save(store)
    assert store.prune(NOW, limit=2) == 2
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_positioning_observations").fetchone()[
                0
            ]
            == 2
        )
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0
    assert store.prune(NOW, limit=2) == 1
    assert store.prune(NOW) == 0


def test_concurrent_retention_is_bounded_and_preserves_recent_rows(store, database):
    seed_old_observations(database, count=20)
    recent = save(store)
    ready = Barrier(2)

    def prune():
        ready.wait(timeout=5)
        return store.prune(NOW, limit=7)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: prune(), range(2)))
    # Concurrent snapshots may select the same rows; the second DELETE then
    # reports zero rather than deleting extra, newer or already removed rows.
    assert all(0 <= count <= 7 for count in results)
    assert 7 <= sum(results) <= 14
    with database() as conn:
        assert conn.execute(
            "SELECT count(*) FROM v2_positioning_observations"
        ).fetchone()[0] == 21 - sum(results)
    assert store.recent("BTCUSDT", before_ms=recent["first_received_at_ms"])


def test_retention_lock_contention_rolls_back_and_can_retry(store, database):
    seed_old_observations(database, count=2)
    recent = save(store)
    with database() as blocker:
        blocker.execute(
            "SELECT observation_id FROM v2_positioning_observations WHERE symbol='OLD0USDT' FOR UPDATE"
        )
        with pytest.raises(psycopg.errors.LockNotAvailable):
            store.prune(NOW, limit=2)
        # Even if a different row was deleted first, timeout rolls it back.
        with database() as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM v2_positioning_observations"
                ).fetchone()[0]
                == 3
            )
    assert store.prune(NOW, limit=2) == 2
    assert store.recent("BTCUSDT", before_ms=recent["first_received_at_ms"])
