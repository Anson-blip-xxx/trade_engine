"""On-chain research QA; database writes require disposable Unix-socket QA."""

import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.types.json import Jsonb
from test_v2_positioning_store import seed_position

from services.v2_onchain_store import (
    DAY_MS,
    MAPPING_VERSION,
    MEMBERSHIP,
    PROVIDER,
    SLOT_MS,
    SOURCE,
    VERSION,
    OnchainStore,
    _prepared,
)
from tests.support.v2_admission import database as database_fixture
from v2_core.evidence import canonical, digest

database = database_fixture
ROOT = Path(__file__).resolve().parents[2]
SLOT = (time.time_ns() // 1_000_000 // SLOT_MS - 2) * SLOT_MS
NOW = SLOT + 100_000


def clock():
    return time.time_ns() // 1_000_000


def mapping(symbol="QUSDT", verified_at_ms=NOW - 1000, **changes):
    value = {
        "version": MAPPING_VERSION,
        "provider": PROVIDER,
        "symbol": symbol,
        "network_id": "bsc",
        "token_address": "0x" + "a" * 40,
        "contract_multiplier": "1",
        "status": "VERIFIED",
        "verified_at_ms": verified_at_ms,
        "valid_until_ms": verified_at_ms + 30 * DAY_MS,
        "source_links": ["https://example.org/official/token"],
        "reason_code": "PRIMARY_SOURCE_VERIFIED",
    }
    value.update(changes)
    return value


def bundle(symbol="QUSDT", started=SLOT + 1, mapping_id=None):
    return {
        "version": VERSION,
        "source": SOURCE,
        "provider": PROVIDER,
        "symbol": symbol,
        "network_id": "bsc" if mapping_id else None,
        "token_address": "0x" + "a" * 40 if mapping_id else None,
        "mapping_id": mapping_id,
        "started_at_ms": started,
        "finished_at_ms": started,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
        "halt_reason": None,
        "retry_after_seconds": None,
        "components": {
            name: {
                "status": "UNKNOWN",
                "endpoint": "/mapping/unavailable",
                "fetched_at_ms": started,
                "payload": None,
                "reason": "MAPPING_UNVERIFIED",
                "retry_after_seconds": None,
                "sha256": None,
            }
            for name in ("token", "pools")
        },
    }


def features(value):
    return {
        **{key: value[key] for key in ("version", "symbol", "network_id", "token_address")},
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
        "status": "UNKNOWN",
        "metrics": {},
        "limitations": ["SINGLE_NETWORK_PARTIAL_COVERAGE"],
    }


def no_database():
    pytest.fail("invalid on-chain research input opened a database connection")


@pytest.fixture
def store(database):
    with database() as conn:
        for filename in (
            "20260925_tenant_registry.sql",
            "20260928_execution_routes.sql",
            "20260928_execution_draining.sql",
            "20261031_onchain_research.sql",
            "20261031_onchain_research.sql",
        ):
            conn.execute((ROOT / "db/migrations" / filename).read_text())
    return OnchainStore(database)


def save(store, symbol="QUSDT", started=SLOT + 1, raw=None, membership=None, **changes):
    value = bundle(symbol, started) if raw is None else raw
    value.update(changes)
    return store.save(
        value,
        slot_ms=started // SLOT_MS * SLOT_MS,
        membership=MEMBERSHIP.copy() if membership is None else membership,
        features=features(value),
    )


@pytest.mark.parametrize("value", [True, -1, 2**53, "123", 0.5])
def test_invalid_clock_cannot_connect(value):
    with pytest.raises(ValueError, match="ONCHAIN_"):
        OnchainStore(no_database).select_symbols(value)


@pytest.mark.parametrize("value", [True, 0, -1, 21, "3"])
def test_invalid_limit_cannot_connect(value):
    with pytest.raises(ValueError, match="ONCHAIN_"):
        OnchainStore(no_database).select_symbols(NOW, value)


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "old"}, {"provider": "OTHER"}, {"symbol": "Q;DROPUSDT"},
        {"status": "AUTO_VERIFIED"}, {"contract_multiplier": "0"},
        {"contract_multiplier": "NaN"}, {"contract_multiplier": 1},
        {"contract_multiplier": "1.5"}, {"contract_multiplier": "1000001"},
        {"network_id": "bad/network"}, {"token_address": "bad/address"},
        {"valid_until_ms": NOW - 1000}, {"source_links": []},
        {"source_links": ["http://example.org/x"]},
        {"source_links": ["https://user:secret@example.org/x"]},
        {"source_links": ["https://example.org/x?api_key=secret"]},
        {"source_links": ["https://example.org/x#secret"]},
        {"reason_code": "raw secret exception"},
        {"account_id": "private"},
    ],
)
def test_invalid_mapping_cannot_connect(changes):
    with pytest.raises(ValueError, match="ONCHAIN_"):
        OnchainStore(no_database).register_mapping(mapping(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"version": "old"}, {"source": "BINANCE"}, {"provider": "OTHER"},
        {"source_host": "evil.example"}, {"symbol": "QUSDC"},
        {"started_at_ms": SLOT - 1}, {"started_at_ms": SLOT + SLOT_MS},
        {"finished_at_ms": SLOT}, {"finished_at_ms": SLOT + 2 * SLOT_MS + 1},
        {"components": {}}, {"account_id": "private"},
        {"execution_authorized": True}, {"promotion_authorized": 0},
        {"mapping_id": "not-uuid"}, {"retry_after_seconds": 86401},
        {"halt_reason": "raw exception"},
    ],
)
def test_invalid_bundle_cannot_connect(changes):
    with pytest.raises(ValueError, match="ONCHAIN_"):
        save(OnchainStore(no_database), **changes)


@pytest.mark.parametrize("key", ["execution_authorized", "automatic_parameter_updates", "promotion_authorized"])
def test_features_must_be_research_only(key):
    value = bundle()
    with pytest.raises(ValueError, match="ONCHAIN_RESEARCH_ONLY"):
        OnchainStore(no_database).save(value, slot_ms=SLOT, membership=MEMBERSHIP,
                                       features={**features(value), key: True})


@pytest.mark.parametrize("key,value", [("symbol", "OTHERUSDT"), ("network_id", "ethereum"), ("token_address", "other")])
def test_features_identity_must_match(key, value):
    raw = bundle()
    with pytest.raises(ValueError, match="ONCHAIN_FEATURE_IDENTITY"):
        OnchainStore(no_database).save(raw, slot_ms=SLOT, membership=MEMBERSHIP,
                                       features={**features(raw), key: value})


@pytest.mark.parametrize("field,value", [("account_id", "private"), ("api_key", "private"), ("ratio", 0.5), ("ratio", float("nan"))])
def test_feature_private_data_and_floats_never_stored(field, value):
    raw = bundle()
    with pytest.raises(ValueError, match="ONCHAIN_"):
        _prepared(raw, SLOT, MEMBERSHIP, {**features(raw), field: value})


@pytest.mark.parametrize(
    "field,value",
    [("payload", {}), ("reason", None), ("reason", "raw exception"),
     ("endpoint", "https://evil.example"), ("fetched_at_ms", SLOT),
     ("sha256", "a" * 64), ("retry_after_seconds", -1)],
)
def test_unknown_component_contract(field, value):
    raw = bundle()
    raw["components"]["token"][field] = value
    with pytest.raises(ValueError, match="ONCHAIN_"):
        _prepared(raw, SLOT, MEMBERSHIP, features(raw))


@pytest.mark.parametrize("members", [[], "ACTIVE_SANDBOX_POSITION", ["FIXED_CONTROL"], ["ACTIVE_SANDBOX_POSITION", "EXPLICIT_RESEARCH_SMOKE"]])
def test_membership_not_invented(members):
    with pytest.raises(ValueError, match="ONCHAIN_MEMBERSHIP"):
        save(OnchainStore(no_database), membership=members)


def test_json_bounds_and_nested_private_field():
    raw = bundle()
    for extra in [{"padding": "x" * 262144}, {"nested": {"credential_ref": "private"}}]:
        with pytest.raises(ValueError, match="ONCHAIN_"):
            _prepared(raw, SLOT, MEMBERSHIP, {**features(raw), **extra})
    nested = {}
    for _ in range(20):
        nested = {"nested": nested}
    with pytest.raises(ValueError, match="ONCHAIN_JSON_BOUND"):
        _prepared(raw, SLOT, MEMBERSHIP, {**features(raw), **nested})


def test_empty_positions_select_nothing(store):
    assert store.select_symbols(NOW) == []


def test_selection_current_sandbox_only_actual_residuals_and_rotation(store, database):
    for symbol in ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"):
        seed_position(database, symbol)
    seed_position(database, "LIVEUSDT", environment="LIVE")
    seed_position(database, "OLDUSDT", bound=False)
    seed_position(database, "EMPTYUSDT", filled=False)
    seed_position(database, "CLOSEDUSDT", closed=True)
    seed_position(database, "STALEUSDT")
    with database() as conn:
        conn.execute("UPDATE v2_tenant_accounts SET version=2 WHERE account_id=(SELECT account_id FROM v2_trade_intents WHERE payload->>'symbol'='STALEUSDT')")
    first = store.select_symbols(NOW, 3)
    assert [row["symbol"] for row in first] == ["AAAUSDT", "BBBUSDT", "CCCUSDT"]
    assert all(set(row) == {"symbol", "membership"} and row["membership"] == MEMBERSHIP for row in first)
    for row in first:
        save(store, row["symbol"])
    assert [row["symbol"] for row in store.select_symbols(NOW, 3)] == ["DDDUSDT"]
    assert store.select_symbols(NOW + SLOT_MS, 3)[0]["symbol"] == "DDDUSDT"


def test_mapping_is_immutable_first_revision_and_temporally_available(store, database):
    first = store.register_mapping(mapping())
    again = store.register_mapping(mapping())
    collision = store.register_mapping(mapping(token_address="0x" + "b" * 40))
    assert first["inserted"] and not again["inserted"] and again["same_input"]
    assert not collision["inserted"] and not collision["same_input"]
    assert store.lookup_mapping("QUSDT", NOW) is None  # historical receipt not proven
    value = store.lookup_mapping("QUSDT", first["first_received_at_ms"])
    assert value["mapping_id"] == first["mapping_id"]
    assert value["token_address"] == "0x" + "a" * 40
    for command in ("UPDATE v2_onchain_mappings SET status='REVOKED'", "DELETE FROM v2_onchain_mappings"):
        with pytest.raises(psycopg.errors.RaiseException, match="IMMUTABLE"), database() as conn:
            conn.execute(command)


def test_latest_revoked_or_expired_mapping_never_falls_back_to_old(store):
    first = store.register_mapping(mapping())
    assert store.lookup_mapping("QUSDT", first["first_received_at_ms"])
    second = store.register_mapping(mapping(verified_at_ms=NOW, status="REVOKED", reason_code="IDENTITY_DISPUTED"))
    assert store.lookup_mapping("QUSDT", second["first_received_at_ms"]) is None
    other = store.register_mapping(mapping("AKEUSDT", valid_until_ms=NOW))
    assert store.lookup_mapping("AKEUSDT", other["first_received_at_ms"]) is None


def observed_bundle(store, symbol="QUSDT"):
    registered = store.register_mapping(mapping(symbol))
    raw = bundle(symbol, registered["first_received_at_ms"], registered["mapping_id"])
    for comp in raw["components"].values():
        comp.update(status="OBSERVED", payload={"price_usd": "1.25"}, reason=None,
                    sha256=digest(canonical({"payload": {"price_usd": "1.25"}})))
    return raw


def test_observed_mapping_link_is_verified_and_exact(store):
    raw = observed_bundle(store)
    row = save(store, started=raw["started_at_ms"], raw=raw)
    result = store.recent("QUSDT", before_ms=row["first_received_at_ms"])[0]
    assert result["bundle"] == raw
    raw["token_address"] = "0x" + "b" * 40
    with pytest.raises(ValueError, match="ONCHAIN_MAPPING_NOT_AVAILABLE"):
        save(store, started=raw["started_at_ms"], raw=raw)


def test_observed_mapping_not_backdated_or_mixed_symbol(store):
    raw = observed_bundle(store)
    for change in ({"symbol": "OTHERUSDT"}, {"network_id": "ethereum"}):
        changed = {**raw, **change}
        with pytest.raises(ValueError, match="ONCHAIN_MAPPING_NOT_AVAILABLE"):
            save(store, started=raw["started_at_ms"], raw=changed)
    earlier = bundle("QUSDT", SLOT + 1, raw["mapping_id"])
    with pytest.raises(ValueError, match="ONCHAIN_MAPPING_NOT_AVAILABLE"):
        save(store, raw=earlier)


def test_old_mapping_rejected_if_superseded_before_collection(store):
    registered = store.register_mapping(mapping())
    replacement = store.register_mapping(mapping(verified_at_ms=NOW, status="REVOKED", reason_code="IDENTITY_DISPUTED"))
    raw = bundle("QUSDT", replacement["first_received_at_ms"], registered["mapping_id"])
    with pytest.raises(ValueError, match="ONCHAIN_MAPPING_SUPERSEDED"):
        save(store, started=raw["started_at_ms"], raw=raw)


def test_first_slot_unknown_is_preserved_and_new_payload_not_overwritten(store):
    first = save(store)
    again = save(store)
    raw = bundle()
    raw["components"]["token"]["reason"] = "LATE_RETRY"
    later = save(store, raw=raw)
    assert first["inserted"] and not again["inserted"] and not later["inserted"]
    assert again["same_input"] and not later["same_input"]
    assert first["observation_id"] == later["observation_id"]
    assert store.recent("QUSDT", before_ms=NOW) == []
    result = store.recent("QUSDT", before_ms=first["first_received_at_ms"])
    assert result[0]["bundle"]["components"]["token"]["reason"] == "MAPPING_UNVERIFIED"


def test_concurrent_first_slot_has_single_record(store):
    with ThreadPoolExecutor(4) as executor:
        rows = list(executor.map(lambda _: save(store), range(4)))
    assert sum(row["inserted"] for row in rows) == 1
    assert len({row["observation_id"] for row in rows}) == 1


def test_explicit_smoke_not_labelled_position(store):
    row = save(store, membership=["EXPLICIT_RESEARCH_SMOKE"])
    assert store.recent("QUSDT", before_ms=row["first_received_at_ms"])[0]["membership"] == ["EXPLICIT_RESEARCH_SMOKE"]


def test_circuit_backoff_server_hint_stale_success_and_recovery(store):
    assert not store.read_circuit(PROVIDER, NOW)["cooldown_active"]
    first = store.record_circuit(PROVIDER, now_ms=NOW, outcome="FAILURE")
    assert first["consecutive_failures"] == 1 and first["retry_after_ms"] == NOW + 60_000
    second = store.record_circuit(PROVIDER, now_ms=NOW + 1, outcome="FAILURE")
    assert second["consecutive_failures"] == 2 and second["retry_after_ms"] == NOW + 120_001
    hinted = store.record_circuit(PROVIDER, now_ms=NOW + 2, outcome="FAILURE", retry_after_ms=NOW + 7_200_000)
    assert hinted["retry_after_ms"] == NOW + 7_200_000
    assert store.record_circuit(PROVIDER, now_ms=NOW, outcome="SUCCESS")["consecutive_failures"] == 3
    assert store.record_circuit(PROVIDER, now_ms=NOW + 2, outcome="SUCCESS")["consecutive_failures"] == 3
    recovered = store.record_circuit(PROVIDER, now_ms=NOW + 3, outcome="SUCCESS")
    assert recovered["consecutive_failures"] == 0 and not recovered["cooldown_active"]


def test_circuit_backoff_is_bounded(store):
    for offset in range(12):
        row = store.record_circuit(PROVIDER, now_ms=NOW + offset, outcome="FAILURE")
    assert row["consecutive_failures"] == 12
    assert row["retry_after_ms"] == NOW + 11 + 1_800_000


@pytest.mark.parametrize("changes", [{"provider": "OTHER"}, {"outcome": "RAW ERROR"}, {"retry_after_ms": NOW - 1}, {"retry_after_ms": NOW + DAY_MS + 1}])
def test_invalid_circuit_never_connects(changes):
    kwargs = {"provider": PROVIDER, "now_ms": NOW, "outcome": "FAILURE", **changes}
    with pytest.raises(ValueError, match="ONCHAIN_"):
        OnchainStore(no_database).record_circuit(**kwargs)


def seed_old(database, count=3):
    for number in range(count):
        old = SLOT - 40 * DAY_MS
        raw = bundle(f"OLD{number}USDT", old + 1)
        _, _, fingerprint = _prepared(raw, old, MEMBERSHIP, features(raw))
        with database() as conn:
            conn.execute(
                "INSERT INTO v2_onchain_observations(observation_id,provider,source,symbol,slot_ms,method_version,bundle,features,membership,input_digest,first_received_at_ms) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (uuid4(), PROVIDER, SOURCE, raw["symbol"], old, VERSION, Jsonb(raw), Jsonb(features(raw)), Jsonb(MEMBERSHIP), fingerprint, old + 2),
            )
            conn.execute(
                "INSERT INTO v2_onchain_runs(run_id,method_version,started_at_ms,completed_at_ms,status,coverage,created_at) VALUES(%s,%s,%s,%s,'PARTIAL','{}',to_timestamp(%s/1000.0))",
                (uuid4(), VERSION, old, old, NOW - 100 * DAY_MS),
            )


def test_bounded_retention_preserves_mappings_recent_rows_and_trading(store, database):
    save(store)
    seed_old(database)
    identity = store.register_mapping(mapping())
    assert store.prune(NOW, limit=2) == {"observations": 2, "runs": 2}
    assert store.prune(NOW, limit=2) == {"observations": 1, "runs": 1}
    assert store.prune(NOW) == {"observations": 0, "runs": 0}
    assert store.lookup_mapping("QUSDT", identity["first_received_at_ms"])
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_onchain_observations").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0


def test_observation_and_run_update_forbidden(store, database):
    save(store)
    store.record_run(started_at_ms=NOW, completed_at_ms=NOW + 1, status="IDLE", coverage={"selected": 0})
    for table in ("v2_onchain_observations", "v2_onchain_runs"):
        with pytest.raises(psycopg.errors.RaiseException, match="IMMUTABLE"), database() as conn:
            conn.execute(f"UPDATE {table} SET method_version=method_version")


def test_least_privilege_role_cannot_modify_trade_or_mapping_read_secrets(store, database):
    role = "codex_onchain_qa_" + uuid4().hex
    with database() as conn:
        schema = conn.execute("SELECT current_schema()").fetchone()[0]
        conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
        for table, columns in {
            "v2_execution_routes": "tenant_id,target_registry,binding_version,environment,phase",
            "v2_tenant_accounts": "tenant_id,registry_id,version,exchange,account_id,environment,product",
            "v2_trade_intents": "intent_id,payload,exchange,account_id,environment,product",
            "v2_episodes": "episode_id,status",
            "v2_orders": "order_id,episode_id,leg",
            "v2_fills": "order_id,quantity",
        }.items():
            conn.execute(sql.SQL("GRANT SELECT ({}) ON {} TO {}").format(
                sql.SQL(",").join(map(sql.Identifier, columns.split(","))), sql.Identifier(table), sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT SELECT ON v2_onchain_mappings TO {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT SELECT,INSERT,DELETE ON v2_onchain_observations,v2_onchain_runs TO {}").format(sql.Identifier(role)))
        conn.execute(sql.SQL("GRANT SELECT,INSERT,UPDATE ON v2_onchain_provider_circuits TO {}").format(sql.Identifier(role)))

    @contextmanager
    def restricted():
        with database() as conn:
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            yield conn

    try:
        seed_position(database, "QUSDT")
        identity = store.register_mapping(mapping())
        research = OnchainStore(restricted)
        assert research.select_symbols(NOW)[0]["symbol"] == "QUSDT"
        assert research.lookup_mapping("QUSDT", identity["first_received_at_ms"])
        row = save(research)
        assert research.recent("QUSDT", before_ms=row["first_received_at_ms"])
        research.record_run(started_at_ms=NOW, completed_at_ms=NOW, status="COMPLETE", coverage={})
        research.record_circuit(PROVIDER, now_ms=NOW, outcome="FAILURE")
        seed_old(database)
        assert research.prune(NOW, limit=2) == {"observations": 2, "runs": 2}
        for query in (
            "SELECT credential_ref FROM v2_tenant_accounts", "SELECT blockers FROM v2_execution_routes",
            "DELETE FROM v2_orders", "DELETE FROM v2_trade_intents",
            "UPDATE v2_onchain_observations SET source=source", "DELETE FROM v2_onchain_mappings",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege), restricted() as conn:
                conn.execute(query)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            research.register_mapping(mapping("AKEUSDT"))
    finally:
        with database() as conn:
            conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
            conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def integration_harness(store, *, status=200):
    from test_v2_onchain_market import Harness, Response, pools, token

    addresses = {"QUSDT": "0x" + "a" * 40, "AKEUSDT": "0x" + "b" * 40}
    for symbol, address in addresses.items():
        store.register_mapping(mapping(symbol, token_address=address))
    now = clock()
    harness = Harness()
    harness.market.clock = clock

    def response():
        if status != 200:
            return Response(status=status, headers={"Retry-After": "120"})
        path = urlsplit(harness.calls[-1][3])
        is_pool = "/pools/search" in path.path
        address = parse_qs(path.query)["token_address"][0] if is_pool else path.path.rsplit("/", 1)[-1]
        if is_pool:
            payload = pools(address=address)
        else:
            payload = token(address=address)
            payload["symbol"] = next(symbol[:-4] for symbol, value in addresses.items() if value == address)
            payload["last_updated"] = datetime.fromtimestamp(now / 1000, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
            for window in ("1m", "5m", "15m", "1h"):
                payload["summary"][window] = {"buy_usd": "12.5", "sell_usd": "7.5", "volume_usd": "20", "buys": 2, "sells": 1, "txns": 3}
        return Response(payload)

    harness.overrides = {"token": response, "pools": response}
    return harness, now


def test_complete_provider_describe_runner_pg_contract_two_explicit_symbols(store, database):
    from services.v2_onchain_entry import run_batch

    harness, now = integration_harness(store)
    report = run_batch(store, lambda: harness.market, now_ms=now,
                       research_symbols=("QUSDT", "AKEUSDT"), free_disk_bytes=100 * 1024**3)
    assert report["status"] == "PARTIAL"  # pools remain partial, not total-market flow
    assert report["inserted"] == 2 and report["attempted"] == 2
    assert len(harness.calls) == 4 and not report["errors"]
    for symbol in ("QUSDT", "AKEUSDT"):
        saved = store.recent(symbol, before_ms=clock())[0]
        assert saved["membership"] == ["EXPLICIT_RESEARCH_SMOKE"]
        assert saved["features"]["metrics"]["provider_aggregate"]["status"] == "OBSERVED_AGGREGATE"
        assert saved["features"]["metrics"]["provider_aggregate"]["windows"]["5m"]["signed_amount_imbalance"] == "0.25"
        assert saved["features"]["metrics"]["pools"]["coverage"] == "PARTIAL"
        assert saved["features"]["execution_authorized"] is False
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_trade_intents").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM v2_onchain_runs").fetchone()[0] == 1


def test_v4_bytes32_pool_id_survives_provider_evidence_runner_and_pg(store):
    from test_v2_onchain_market import Response, pools

    from services.v2_onchain_entry import run_batch

    harness, now = integration_harness(store)
    target_token = "0x" + "a" * 40
    address_pool = "0x" + "e" * 40
    v4_pool_id = "0x478064492057be4bb5a34f6570215260f66997d5a939b2ec44583e5c4c29a618"
    payload = pools(address=target_token)
    payload["results"].append({
        **payload["results"][0],
        "id": v4_pool_id,
        "dex_id": "uniswap_v4",
        "dex_name": "Uniswap V4",
    })
    harness.overrides["pools"] = lambda: Response(payload)

    report = run_batch(store, lambda: harness.market, now_ms=now,
                       research_symbols=("QUSDT",), free_disk_bytes=100 * 1024**3)
    assert report["status"] == "PARTIAL" and report["inserted"] == 1
    assert len(harness.calls) == 2 and not report["errors"]
    saved = store.recent("QUSDT", before_ms=clock())[0]
    assert saved["features"]["metrics"]["provider_aggregate"]["status"] == "OBSERVED_AGGREGATE"
    pool_features = saved["features"]["metrics"]["pools"]
    assert pool_features["status"] == "PARTIAL" and pool_features["coverage"] == "PARTIAL"
    selected = {row["pool_id"]: row for row in pool_features["reported_pools"]}
    assert set(selected) == {address_pool, v4_pool_id}
    assert selected[address_pool]["pool_address"] == address_pool
    assert selected[address_pool]["identity_kind"] == "ADDRESS"
    assert selected[v4_pool_id]["pool_address"] is None
    assert selected[v4_pool_id]["identity_kind"] == "BYTES32_POOL_ID"
    assert selected[v4_pool_id]["dex_id"] == "uniswap_v4"
    raw = saved["bundle"]["components"]["pools"]["payload"]["results"]
    assert {row["id"] for row in raw} == {address_pool, v4_pool_id}
    assert all(len(token["id"]) == 42 for row in raw for token in row["tokens"])
    assert saved["bundle"]["token_address"] == target_token
    assert saved["features"]["execution_authorized"] is False


def test_provider_rate_limit_persists_unknown_and_circuit_next_run_no_http(store, database):
    from services.v2_onchain_entry import run_batch

    harness, now = integration_harness(store, status=429)
    report = run_batch(store, lambda: harness.market, now_ms=now,
                       research_symbols=("QUSDT", "AKEUSDT"), free_disk_bytes=100 * 1024**3)
    assert report["status"] == "DEFERRED" and report["inserted"] == 1
    assert len(harness.calls) == 1 and report["deferred_symbols"] == 1
    saved = store.recent("QUSDT", before_ms=clock())[0]
    assert saved["features"]["status"] == "UNKNOWN"
    report2 = run_batch(store, no_database, now_ms=clock(),
                        research_symbols=("QUSDT", "AKEUSDT"), free_disk_bytes=100 * 1024**3)
    assert report2["status"] == "COOLDOWN" and report2["attempted"] == 0
    assert len(harness.calls) == 1
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_onchain_runs").fetchone()[0] == 2


def test_no_positions_is_idle_without_market_factory_or_guessed_mapping(store, database):
    from services.v2_onchain_entry import run_batch

    report = run_batch(store, no_database, now_ms=clock(), free_disk_bytes=100 * 1024**3)
    assert report["status"] == "IDLE" and report["selected"] == 0
    with database() as conn:
        assert conn.execute("SELECT count(*) FROM v2_onchain_observations").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM v2_onchain_runs").fetchone()[0] == 1


def test_unmapped_positions_do_not_starve_verified_position(store, database):
    from services.v2_onchain_entry import run_batch

    for symbol in ("AAAUSDT", "BBBUSDT", "CCCUSDT", "QUSDT"):
        seed_position(database, symbol)
    harness, now = integration_harness(store)
    first = run_batch(store, no_database, now_ms=now, free_disk_bytes=100 * 1024**3)
    assert first["status"] == "PARTIAL" and first["inserted"] == 0 and first["attempted"] == 0
    assert first["outcome_counts"] == {"UNKNOWN": 3}
    second = run_batch(store, lambda: harness.market, now_ms=clock(), free_disk_bytes=100 * 1024**3)
    assert second["selected"] == 1 and second["inserted"] == 1
    assert second["outcomes"][0]["symbol"] == "QUSDT"
    assert len(harness.calls) == 2


def test_only_actual_run_outcomes_count_as_visited_not_deferred_selection(store, database):
    for symbol in ("AAAUSDT", "BBBUSDT", "CCCUSDT", "DDDUSDT"):
        seed_position(database, symbol)
    store.record_run(started_at_ms=NOW, completed_at_ms=NOW + 1, status="DEFERRED", coverage={
        "scope": "ACTIVE_SANDBOX_POSITION", "selected": 3, "deferred_symbols": 2,
        "outcomes": [{"symbol": "AAAUSDT", "status": "UNKNOWN"}],
    })
    # A run persisted now cannot be backdated into historical selection.
    assert store.select_symbols(NOW + 2, 3)[0]["symbol"] == "AAAUSDT"
    selected = store.select_symbols(clock(), 3)
    assert [row["symbol"] for row in selected] == ["BBBUSDT", "CCCUSDT", "DDDUSDT"]
