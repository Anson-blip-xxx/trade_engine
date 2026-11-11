"""Bounded, credential-free PostgreSQL storage for an optional research lane.

Only current SANDBOX filled residuals are selected. No account identifiers leave
selection SQL. First receipt wins each slot, including UNKNOWN observations.
This module has no execution, exchange-account or business-state write port.
"""

import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from v2_core.evidence import canonical, digest

VERSION = "onchain-public-observation-v1"
MAPPING_VERSION = "onchain-asset-map-v1"
SOURCE = "DEXPAPRIKA_PUBLIC_KEYLESS"
PROVIDER = "DEXPAPRIKA"
SLOT_MS = 300_000
DAY_MS = 86_400_000
MAX_PAYLOAD_BYTES = 262_144
MEMBERSHIP = ["ACTIVE_SANDBOX_POSITION"]
RUN_STATUSES = frozenset(
    {"COMPLETE", "PARTIAL", "FAILED", "NO_SYMBOLS", "IDLE", "COOLDOWN", "RATE_LIMITED", "SKIPPED", "DEFERRED"}
)
AUTH_FIELDS = ("execution_authorized", "automatic_parameter_updates", "promotion_authorized")
_PRIVATE_FIELDS = frozenset(
    {"accountid", "tenantid", "registryid", "credentialref", "credentials",
     "accountalias", "clientorderid", "orderid", "secret", "apikey", "apisecret",
     "password", "authorization", "bottoken", "accesstoken", "refreshtoken"}
)


def _ms(value):
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("ONCHAIN_TIMESTAMP_INVALID")
    return value


def _symbol(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Z0-9_]{1,40}USDT", value):
        raise ValueError("ONCHAIN_SYMBOL_INVALID")
    return value


def _code(value):
    if value is not None and (
        not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", value)
    ):
        raise ValueError("ONCHAIN_ERROR_CODE_INVALID")
    return value


def _json(value, *, maximum=MAX_PAYLOAD_BYTES):
    count = 0

    def visit(item, depth=0):
        nonlocal count
        count += 1
        if depth > 15 or count > 20_000:
            raise ValueError("ONCHAIN_JSON_BOUND")
        if isinstance(item, dict):
            if len(item) > 200 or any(not isinstance(k, str) for k in item):
                raise ValueError("ONCHAIN_JSON_BOUND")
            for key, child in item.items():
                if "".join(c for c in key.lower() if c.isalnum()) in _PRIVATE_FIELDS:
                    raise ValueError("ONCHAIN_PRIVATE_DATA_FORBIDDEN")
                if len(key) > 100:
                    raise ValueError("ONCHAIN_JSON_BOUND")
                visit(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > 1000:
                raise ValueError("ONCHAIN_JSON_BOUND")
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, str) and len(item) > maximum:
            raise ValueError("ONCHAIN_JSON_BOUND")

    visit(value)
    try:
        encoded = canonical(value)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("ONCHAIN_JSON_INVALID") from exc
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError("ONCHAIN_JSON_BOUND")
    return encoded


def _mapping_prepared(mapping):
    required = {
        "version", "provider", "symbol", "network_id", "token_address",
        "contract_multiplier", "status", "verified_at_ms", "valid_until_ms",
        "source_links", "reason_code",
    }
    if not isinstance(mapping, dict) or set(mapping) != required:
        raise ValueError("ONCHAIN_MAPPING_INVALID")
    _symbol(mapping["symbol"])
    if mapping["version"] != MAPPING_VERSION or mapping["provider"] != PROVIDER:
        raise ValueError("ONCHAIN_MAPPING_SOURCE_INVALID")
    if mapping["status"] not in {"VERIFIED", "UNSUPPORTED", "REVOKED"}:
        raise ValueError("ONCHAIN_MAPPING_STATUS_INVALID")
    _code(mapping["reason_code"])
    start, end = _ms(mapping["verified_at_ms"]), _ms(mapping["valid_until_ms"])
    if not start < end <= start + 366 * DAY_MS:
        raise ValueError("ONCHAIN_MAPPING_VALIDITY_INVALID")
    if not isinstance(mapping["network_id"], str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]{0,49}", mapping["network_id"]
    ):
        raise ValueError("ONCHAIN_MAPPING_NETWORK_INVALID")
    if not isinstance(mapping["token_address"], str) or not re.fullmatch(
        r"[A-Za-z0-9:._-]{1,128}", mapping["token_address"]
    ):
        raise ValueError("ONCHAIN_MAPPING_ADDRESS_INVALID")
    try:
        multiplier = Decimal(mapping["contract_multiplier"])
        if (not isinstance(mapping["contract_multiplier"], str)
            or not multiplier.is_finite() or not 0 < multiplier <= 1_000_000
            or multiplier != multiplier.to_integral_value()):
            raise ValueError("ONCHAIN_MAPPING_MULTIPLIER_INVALID")
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("ONCHAIN_MAPPING_MULTIPLIER_INVALID") from exc
    links = mapping["source_links"]
    if not isinstance(links, list) or not 1 <= len(links) <= 10:
        raise ValueError("ONCHAIN_MAPPING_SOURCES_INVALID")
    for link in links:
        if not isinstance(link, str) or len(link) > 1024:
            raise ValueError("ONCHAIN_MAPPING_SOURCES_INVALID")
        url = urlsplit(link)
        if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.port not in (None, 443) or url.query or url.fragment):
            raise ValueError("ONCHAIN_MAPPING_SOURCES_INVALID")
    if len(set(links)) != len(links):
        raise ValueError("ONCHAIN_MAPPING_SOURCES_INVALID")
    return digest(_json(mapping, maximum=16_384))


def _prepared(bundle, slot_ms, membership, features):
    _ms(slot_ms)
    if slot_ms % SLOT_MS:
        raise ValueError("ONCHAIN_SLOT_INVALID")
    required = {"version", "source", "provider", "symbol", "network_id", "token_address", "mapping_id", "started_at_ms", "finished_at_ms", "components", *AUTH_FIELDS}
    optional = {"source_host", "halt_reason", "retry_after_seconds"}
    if (not isinstance(bundle, dict) or not required <= bundle.keys()
        or set(bundle) - required - optional):
        raise ValueError("ONCHAIN_BUNDLE_INVALID")
    if (bundle["version"] != VERSION or bundle["source"] != SOURCE
        or bundle.get("provider", PROVIDER) != PROVIDER
        or bundle.get("source_host", "api.dexpaprika.com") != "api.dexpaprika.com"):
        raise ValueError("ONCHAIN_SOURCE_INVALID")
    if any(bundle.get(field) is not False for field in AUTH_FIELDS):
        raise ValueError("ONCHAIN_RESEARCH_ONLY_REQUIRED")
    _code(bundle.get("halt_reason"))
    retry_after = bundle.get("retry_after_seconds")
    if retry_after is not None and (type(retry_after) is not int or not 0 <= retry_after <= 86400):
        raise ValueError("ONCHAIN_RETRY_AFTER_INVALID")
    for field in ("network_id", "token_address"):
        if bundle[field] is not None and (not isinstance(bundle[field], str) or len(bundle[field]) > 128):
            raise ValueError("ONCHAIN_MAPPING_IDENTITY_INVALID")
    symbol = _symbol(bundle["symbol"])
    started, finished = _ms(bundle["started_at_ms"]), _ms(bundle["finished_at_ms"])
    if not slot_ms <= started < slot_ms + SLOT_MS or not started <= finished <= slot_ms + 2 * SLOT_MS:
        raise ValueError("ONCHAIN_SLOT_TIME_MISMATCH")
    mapping_id = bundle.get("mapping_id")
    if mapping_id is not None:
        try:
            mapping_id = str(UUID(mapping_id))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("ONCHAIN_MAPPING_ID_INVALID") from exc
    components = bundle["components"]
    if not isinstance(components, dict) or set(components) != {"token", "pools"}:
        raise ValueError("ONCHAIN_COMPONENTS_INVALID")
    for name, value in components.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z_]{1,50}", name):
            raise ValueError("ONCHAIN_COMPONENTS_INVALID")
        fields = {"status", "endpoint", "fetched_at_ms", "payload", "reason", "retry_after_seconds", "sha256"}
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("ONCHAIN_COMPONENT_INVALID")
        if (value["status"] not in {"OBSERVED", "UNKNOWN"}
            or not isinstance(value["endpoint"], str)
            or not re.fullmatch(r"/[A-Za-z0-9/_.:-]{1,250}", value["endpoint"])
            or not started <= _ms(value["fetched_at_ms"]) <= finished):
            raise ValueError("ONCHAIN_COMPONENT_INVALID")
        if value["status"] == "UNKNOWN":
            if value["payload"] is not None or value["reason"] is None:
                raise ValueError("ONCHAIN_UNKNOWN_REASON_REQUIRED")
            _code(value["reason"])
        elif value["reason"] is not None or mapping_id is None or not isinstance(value["payload"], (dict,list)):
            raise ValueError("ONCHAIN_OBSERVED_MAPPING_REQUIRED")
        component_retry = value["retry_after_seconds"]
        if component_retry is not None and (type(component_retry) is not int or not 0 <= component_retry <= 86400):
            raise ValueError("ONCHAIN_RETRY_AFTER_INVALID")
        if value["sha256"] is not None and value["sha256"] != digest(_json({"payload": value["payload"]})):
            raise ValueError("ONCHAIN_COMPONENT_DIGEST_MISMATCH")
        if value["status"] == "OBSERVED" and value["sha256"] is None:
            raise ValueError("ONCHAIN_COMPONENT_DIGEST_REQUIRED")
    if not isinstance(features, dict) or any(features.get(field) is not False for field in AUTH_FIELDS):
        raise ValueError("ONCHAIN_RESEARCH_ONLY_REQUIRED")
    if any(features.get(field) != bundle[field] for field in ("version", "symbol", "network_id", "token_address")):
        raise ValueError("ONCHAIN_FEATURE_IDENTITY_MISMATCH")
    if membership not in (MEMBERSHIP, ["EXPLICIT_RESEARCH_SMOKE"]):
        raise ValueError("ONCHAIN_MEMBERSHIP_INVALID")
    fingerprint = digest(_json({"bundle": bundle, "features": features, "membership": membership, "slot_ms": slot_ms}))
    return symbol, mapping_id, fingerprint


_COLUMNS = "observation_id::text,slot_ms,bundle,features,membership,first_received_at_ms,input_digest"
_KEYS = ("observation_id", "slot_ms", "bundle", "features", "membership", "first_received_at_ms", "input_digest")


class OnchainStore:
    def __init__(self, connect):
        self.connect = connect

    def select_symbols(self, now_ms, limit=3):
        _ms(now_ms)
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("ONCHAIN_SELECTION_LIMIT")
        with self.connect() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout='1500ms'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            rows = conn.execute(
                """WITH recent_runs AS MATERIALIZED (
                  SELECT started_at_ms,coverage FROM v2_onchain_runs
                  WHERE method_version=%s AND started_at_ms BETWEEN %s AND %s
                    AND completed_at_ms<=%s
                    AND floor(extract(epoch FROM created_at)*1000)::bigint<=%s
                  ORDER BY completed_at_ms DESC,run_id LIMIT 300),
                visits AS MATERIALIZED (
                  SELECT outcome->>'symbol' symbol,max(started_at_ms) last_seen
                  FROM recent_runs CROSS JOIN LATERAL jsonb_array_elements(
                    CASE WHEN jsonb_typeof(coverage->'outcomes')='array' THEN coverage->'outcomes' ELSE '[]'::jsonb END) outcome
                  WHERE coverage->>'scope'='ACTIVE_SANDBOX_POSITION'
                  GROUP BY outcome->>'symbol'),
                positions AS MATERIALIZED (
                  SELECT DISTINCT i.payload->>'symbol' symbol
                  FROM v2_execution_routes r JOIN v2_tenant_accounts a
                    ON (a.tenant_id,a.registry_id)=(r.tenant_id,r.target_registry)
                    AND a.version=r.binding_version
                  JOIN v2_trade_intents i ON (i.exchange,i.account_id,i.environment,i.product)=
                    (a.exchange,a.account_id,a.environment,a.product)
                  JOIN v2_episodes e ON e.episode_id=i.intent_id AND e.status='ACTIVE'
                  WHERE r.environment='SANDBOX' AND a.environment='SANDBOX'
                    AND a.exchange='BINANCE' AND a.product='FUTURES'
                    AND r.phase IN ('ACTIVE','DRAINING')
                    AND i.payload->>'symbol' ~ '^[A-Z0-9_]{1,40}USDT$'
                    AND (SELECT coalesce(sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END),0)
                      FROM v2_orders o JOIN v2_fills f USING(order_id) WHERE o.episode_id=i.intent_id)>0)
                SELECT p.symbol FROM positions p LEFT JOIN LATERAL (
                  SELECT slot_ms FROM v2_onchain_observations x
                  WHERE x.symbol=p.symbol AND x.provider=%s AND x.source=%s AND x.method_version=%s
                  ORDER BY slot_ms DESC LIMIT 1) last ON true
                LEFT JOIN visits ON visits.symbol=p.symbol
                WHERE greatest(last.slot_ms,visits.last_seen) IS NULL
                  OR greatest(last.slot_ms,visits.last_seen)<%s
                ORDER BY greatest(last.slot_ms,visits.last_seen) NULLS FIRST,p.symbol LIMIT %s""",
                (VERSION, max(0, now_ms - DAY_MS), now_ms, now_ms, now_ms,
                 PROVIDER, SOURCE, VERSION, now_ms // SLOT_MS * SLOT_MS, limit),
            ).fetchall()
        return [{"symbol": row[0], "membership": MEMBERSHIP.copy()} for row in rows]

    def register_mapping(self, mapping):
        """Deployment-only port; the runtime observer must lack INSERT grant."""
        fingerprint = _mapping_prepared(mapping)
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='1500ms'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            inserted = conn.execute(
                "INSERT INTO v2_onchain_mappings(mapping_id,provider,symbol,method_version,verified_at_ms,valid_until_ms,status,mapping,input_digest) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(provider,symbol,method_version,verified_at_ms) DO NOTHING RETURNING mapping_id",
                (uuid4(), PROVIDER, mapping["symbol"], MAPPING_VERSION, mapping["verified_at_ms"],
                 mapping["valid_until_ms"], mapping["status"], Jsonb(mapping), fingerprint),
            ).fetchone()
            row = conn.execute(
                "SELECT mapping_id::text,input_digest,first_received_at_ms FROM v2_onchain_mappings "
                "WHERE provider=%s AND symbol=%s AND method_version=%s AND verified_at_ms=%s",
                (PROVIDER, mapping["symbol"], MAPPING_VERSION, mapping["verified_at_ms"]),
            ).fetchone()
        return {"mapping_id": row[0], "inserted": inserted is not None, "same_input": row[1] == fingerprint,
                "input_digest": row[1], "first_received_at_ms": row[2]}

    def lookup_mapping(self, symbol, now_ms):
        _symbol(symbol)
        _ms(now_ms)
        with self.connect() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout='1500ms'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            row = conn.execute(
                "SELECT mapping_id::text,mapping,input_digest,first_received_at_ms FROM v2_onchain_mappings "
                "WHERE provider=%s AND symbol=%s AND method_version=%s AND verified_at_ms<=%s AND first_received_at_ms<=%s "
                "ORDER BY verified_at_ms DESC,first_received_at_ms DESC LIMIT 1",
                (PROVIDER, symbol, MAPPING_VERSION, now_ms, now_ms),
            ).fetchone()
        if row is None or row[1]["status"] != "VERIFIED" or not now_ms < row[1]["valid_until_ms"]:
            return None
        _mapping_prepared(row[1])
        return {**row[1], "mapping_id": row[0], "input_digest": row[2], "first_received_at_ms": row[3]}

    def save(self, bundle, *, slot_ms, membership, features):
        symbol, mapping_id, fingerprint = _prepared(bundle, slot_ms, membership, features)
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='1500ms'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            if mapping_id is not None:
                row = conn.execute(
                    "SELECT symbol,status,verified_at_ms,valid_until_ms,first_received_at_ms,mapping FROM v2_onchain_mappings WHERE mapping_id=%s",
                    (mapping_id,),
                ).fetchone()
                if (row is None or row[0] != symbol or row[1] != "VERIFIED"
                    or not row[2] <= bundle["started_at_ms"] <= bundle["finished_at_ms"] < row[3]
                    or row[4] > bundle["started_at_ms"]
                    or row[5]["network_id"] != bundle["network_id"]
                    or row[5]["token_address"] != bundle["token_address"]):
                    raise ValueError("ONCHAIN_MAPPING_NOT_AVAILABLE_AT_COLLECTION")
                latest = conn.execute(
                    "SELECT mapping_id::text FROM v2_onchain_mappings WHERE provider=%s AND symbol=%s AND method_version=%s "
                    "AND verified_at_ms<=%s AND first_received_at_ms<=%s ORDER BY verified_at_ms DESC,first_received_at_ms DESC LIMIT 1",
                    (PROVIDER, symbol, MAPPING_VERSION, bundle["started_at_ms"], bundle["started_at_ms"]),
                ).fetchone()
                if latest is None or latest[0] != mapping_id:
                    raise ValueError("ONCHAIN_MAPPING_SUPERSEDED_AT_COLLECTION")
            inserted = conn.execute(
                "INSERT INTO v2_onchain_observations(observation_id,provider,source,symbol,slot_ms,method_version,mapping_id,bundle,features,membership,input_digest) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(provider,source,symbol,slot_ms,method_version) DO NOTHING RETURNING observation_id",
                (uuid4(), PROVIDER, SOURCE, symbol, slot_ms, VERSION, mapping_id, Jsonb(bundle), Jsonb(features), Jsonb(membership), fingerprint),
            ).fetchone()
            row = conn.execute(
                "SELECT observation_id::text,input_digest,first_received_at_ms FROM v2_onchain_observations "
                "WHERE provider=%s AND source=%s AND symbol=%s AND slot_ms=%s AND method_version=%s",
                (PROVIDER, SOURCE, symbol, slot_ms, VERSION),
            ).fetchone()
        return {"observation_id": row[0], "inserted": inserted is not None,
                "same_input": row[1] == fingerprint, "input_digest": row[1], "first_received_at_ms": row[2]}

    def recent(self, symbol, *, before_ms, limit=24):
        _symbol(symbol)
        _ms(before_ms)
        if type(limit) is not int or not 1 <= limit <= 288:
            raise ValueError("ONCHAIN_RECENT_LIMIT")
        with self.connect() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout='1500ms'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM v2_onchain_observations WHERE provider=%s AND source=%s AND symbol=%s AND method_version=%s "
                "AND slot_ms<=%s AND first_received_at_ms<=%s ORDER BY slot_ms DESC LIMIT %s",
                (PROVIDER, SOURCE, symbol, VERSION, before_ms, before_ms, limit),
            ).fetchall()
        return [dict(zip(_KEYS, row, strict=True)) for row in rows]

    def record_run(self, *, started_at_ms, completed_at_ms, status, coverage, error_code=None):
        _ms(started_at_ms)
        _ms(completed_at_ms)
        if not started_at_ms <= completed_at_ms <= started_at_ms + 3_600_000 or status not in RUN_STATUSES:
            raise ValueError("ONCHAIN_RUN_INVALID")
        _code(error_code)
        _json(coverage, maximum=16_384)
        identity = uuid4()
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='1500ms'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            conn.execute(
                "INSERT INTO v2_onchain_runs(run_id,method_version,started_at_ms,completed_at_ms,status,coverage,error_code) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (identity, VERSION, started_at_ms, completed_at_ms, status, Jsonb(coverage), error_code),
            )
        return str(identity)

    def read_circuit(self, provider, now_ms):
        if provider != PROVIDER:
            raise ValueError("ONCHAIN_PROVIDER_INVALID")
        _ms(now_ms)
        with self.connect() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout='1500ms'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            row = conn.execute(
                "SELECT consecutive_failures,retry_after_ms,updated_at_ms,outcome FROM v2_onchain_provider_circuits WHERE provider=%s",
                (provider,),
            ).fetchone()
        values = row or (0, 0, 0, "SUCCESS")
        return {"provider": provider, "consecutive_failures": values[0], "retry_after_ms": values[1],
                "updated_at_ms": values[2], "outcome": values[3], "cooldown_active": now_ms < values[1]}

    def record_circuit(self, provider, *, now_ms, outcome, retry_after_ms=None):
        if provider != PROVIDER:
            raise ValueError("ONCHAIN_PROVIDER_INVALID")
        _ms(now_ms)
        if outcome not in {"SUCCESS", "FAILURE"}:
            raise ValueError("ONCHAIN_CIRCUIT_OUTCOME_INVALID")
        if retry_after_ms is not None and not now_ms <= _ms(retry_after_ms) <= now_ms + DAY_MS:
            raise ValueError("ONCHAIN_CIRCUIT_RETRY_INVALID")
        requested = retry_after_ms or now_ms
        # Atomic upsert serializes this research table only; bounded lock wait.
        # Stale requests cannot clear a newer failure, and equal-time failures
        # dominate successes. Circuit persistence failure is surfaced to runner.
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='1500ms'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            conn.execute(
                """INSERT INTO v2_onchain_provider_circuits(provider,consecutive_failures,retry_after_ms,updated_at_ms,outcome)
                VALUES(%s,CASE WHEN %s='SUCCESS' THEN 0 ELSE 1 END,
                  CASE WHEN %s='SUCCESS' THEN %s ELSE greatest(%s,%s+60000) END,%s,%s)
                ON CONFLICT(provider) DO UPDATE SET
                  consecutive_failures=CASE WHEN excluded.outcome='SUCCESS' THEN 0 ELSE least(v2_onchain_provider_circuits.consecutive_failures+1,100) END,
                  retry_after_ms=CASE WHEN excluded.outcome='SUCCESS' THEN excluded.updated_at_ms ELSE
                    greatest(v2_onchain_provider_circuits.retry_after_ms,excluded.retry_after_ms,
                      excluded.updated_at_ms+least(1800000::bigint,60000::bigint*(1::bigint<<least(v2_onchain_provider_circuits.consecutive_failures,5)))) END,
                  updated_at_ms=excluded.updated_at_ms,outcome=excluded.outcome
                WHERE excluded.updated_at_ms>v2_onchain_provider_circuits.updated_at_ms
                  OR (excluded.updated_at_ms=v2_onchain_provider_circuits.updated_at_ms AND excluded.outcome<>'SUCCESS')""",
                (provider, outcome, outcome, now_ms, requested, now_ms, now_ms, outcome),
            )
        return self.read_circuit(provider, now_ms)

    def prune(self, now_ms, *, limit=500):
        _ms(now_ms)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("ONCHAIN_PRUNE_LIMIT")
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='1500ms'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            observations = conn.execute(
                "WITH old AS (SELECT observation_id FROM v2_onchain_observations WHERE first_received_at_ms<%s "
                "ORDER BY first_received_at_ms,observation_id LIMIT %s) DELETE FROM v2_onchain_observations x USING old WHERE x.observation_id=old.observation_id",
                (max(0, now_ms - 30 * DAY_MS), limit),
            ).rowcount
            runs = conn.execute(
                "WITH old AS (SELECT run_id FROM v2_onchain_runs WHERE created_at<to_timestamp(%s/1000.0) "
                "ORDER BY created_at,run_id LIMIT %s) DELETE FROM v2_onchain_runs x USING old WHERE x.run_id=old.run_id",
                (max(0, now_ms - 90 * DAY_MS), limit),
            ).rowcount
        return {"observations": observations, "runs": runs}
