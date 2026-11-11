"""PG-only public positioning research; never an order or policy write port.

Selection reads an already saved catalog and current SANDBOX filled inventory.
The first stored observation wins a five-minute slot, including UNKNOWN data.
Database receipt time is retained separately from caller-supplied fetch times.
"""

import re
from uuid import uuid4

from psycopg.types.json import Jsonb

from v2_core.evidence import canonical, digest

VERSION = "positioning-observation-v1"
SOURCE = "BINANCE_USDM_LIVE_PUBLIC"
SLOT_MS = 300_000
DAY_MS = 86_400_000
MAX_PAYLOAD_BYTES = 262_144
MAX_CANDIDATES = 80
MEMBERSHIPS = frozenset(
    {"FIXED_CONTROL", "TOP_MOVER_DIAGNOSTIC", "ACTIVE_SANDBOX_POSITION"}
)
RUN_STATUSES = frozenset(
    {
        "COMPLETE",
        "COMPLETED",
        "PARTIAL",
        "FAILED",
        "NO_CATALOG",
        "NO_SYMBOLS",
        "RATE_LIMITED",
        "SKIPPED",
        "DEFERRED",
    }
)
_PRIVATE_FIELDS = frozenset(
    {
        "accountid",
        "tenantid",
        "registryid",
        "credentialref",
        "credentials",
        "accountalias",
        "clientorderid",
        "orderid",
        "secret",
        "apikey",
        "apisecret",
        "password",
        "authorization",
        "token",
        "bottoken",
    }
)


def _ms(value):
    if type(value) is not int or not 0 <= value < 2**53:
        raise ValueError("POSITIONING_TIMESTAMP_INVALID")
    return value


def _symbol(value):
    if (
        not isinstance(value, str)
        or not 5 <= len(value) <= 44
        or not value.endswith("USDT")
        or not all(c.isalnum() or c == "_" for c in value)
    ):
        raise ValueError("POSITIONING_SYMBOL_INVALID")
    return value


def _json(value, *, maximum=MAX_PAYLOAD_BYTES):
    """Reject private identities, unsafe JSON and unbounded trees before I/O."""
    count = 0

    def visit(item, depth=0):
        nonlocal count
        count += 1
        if depth > 15 or count > 20_000:
            raise ValueError("POSITIONING_JSON_BOUND")
        if isinstance(item, dict):
            if len(item) > 200 or any(not isinstance(k, str) for k in item):
                raise ValueError("POSITIONING_JSON_BOUND")
            for key, child in item.items():
                if "".join(c for c in key.lower() if c.isalnum()) in _PRIVATE_FIELDS:
                    raise ValueError("POSITIONING_PRIVATE_DATA_FORBIDDEN")
                if len(key) > 100:
                    raise ValueError("POSITIONING_JSON_BOUND")
                visit(child, depth + 1)
        elif isinstance(item, list):
            if len(item) > 1000:
                raise ValueError("POSITIONING_JSON_BOUND")
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, str) and len(item) > maximum:
            raise ValueError("POSITIONING_JSON_BOUND")

    visit(value)
    try:
        encoded = canonical(value)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError("POSITIONING_JSON_INVALID") from exc
    if len(encoded.encode("utf-8")) > maximum:
        raise ValueError("POSITIONING_JSON_BOUND")
    return encoded


def _membership(values):
    if (
        not isinstance(values, list)
        or not values
        or len(values) > 3
        or any(v not in MEMBERSHIPS for v in values)
        or len(set(values)) != len(values)
    ):
        raise ValueError("POSITIONING_MEMBERSHIP_INVALID")
    return sorted(values)


def _prepared(bundle, slot_ms, membership, features):
    _ms(slot_ms)
    if slot_ms % SLOT_MS:
        raise ValueError("POSITIONING_SLOT_INVALID")
    required = {
        "version",
        "source",
        "symbol",
        "started_at_ms",
        "finished_at_ms",
        "components",
    }
    optional = {
        "source_host",
        "execution_authorized",
        "automatic_parameter_updates",
        "promotion_authorized",
        "halt_reason",
    }
    if (
        not isinstance(bundle, dict)
        or not required <= bundle.keys()
        or set(bundle) - required - optional
    ):
        raise ValueError("POSITIONING_BUNDLE_INVALID")
    if bundle.get("source_host", "fapi.binance.com") != "fapi.binance.com":
        raise ValueError("POSITIONING_SOURCE_HOST_INVALID")
    if any(
        bundle.get(key, False) is not False
        for key in (
            "execution_authorized",
            "automatic_parameter_updates",
            "promotion_authorized",
        )
    ):
        raise ValueError("POSITIONING_RESEARCH_ONLY_REQUIRED")
    halt = bundle.get("halt_reason")
    if halt is not None and (
        not isinstance(halt, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", halt)
    ):
        raise ValueError("POSITIONING_HALT_REASON_INVALID")
    if bundle["version"] != VERSION or bundle["source"] != SOURCE:
        raise ValueError("POSITIONING_SOURCE_OR_VERSION_INVALID")
    symbol = _symbol(bundle["symbol"])
    started, finished = _ms(bundle["started_at_ms"]), _ms(bundle["finished_at_ms"])
    if (
        not slot_ms <= started < slot_ms + SLOT_MS
        or not started <= finished <= slot_ms + 2 * SLOT_MS
    ):
        raise ValueError("POSITIONING_SLOT_TIME_MISMATCH")
    components = bundle["components"]
    if not isinstance(components, dict) or not 1 <= len(components) <= 12:
        raise ValueError("POSITIONING_COMPONENTS_INVALID")
    for name, value in components.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z_]{1,50}", name):
            raise ValueError("POSITIONING_COMPONENTS_INVALID")
        component_required = {
            "status",
            "source",
            "endpoint",
            "fetched_at_ms",
            "rows",
            "reason",
            "sha256",
        }
        if (
            not isinstance(value, dict)
            or not component_required <= value.keys()
            or set(value) - component_required - {"source_host"}
        ):
            raise ValueError("POSITIONING_COMPONENT_INVALID")
        if value.get("source_host", "fapi.binance.com") != "fapi.binance.com":
            raise ValueError("POSITIONING_SOURCE_HOST_INVALID")
        if (
            value["status"] not in {"OBSERVED", "UNKNOWN"}
            or not isinstance(value["source"], str)
            or not re.fullmatch(r"[A-Z0-9_]{1,80}", value["source"])
            or not isinstance(value["endpoint"], str)
            or not re.fullmatch(r"/[A-Za-z0-9/_]{1,120}", value["endpoint"])
            or not isinstance(value["rows"], list)
            or len(value["rows"]) > 512
            or not started <= _ms(value["fetched_at_ms"]) <= finished
        ):
            raise ValueError("POSITIONING_COMPONENT_INVALID")
        if value["status"] == "UNKNOWN":
            if (
                value["rows"]
                or not isinstance(value["reason"], str)
                or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", value["reason"])
            ):
                raise ValueError("POSITIONING_UNKNOWN_REASON_REQUIRED")
        elif value["reason"] is not None:
            raise ValueError("POSITIONING_OBSERVED_REASON_INVALID")
        if value["sha256"] is not None and (
            not isinstance(value["sha256"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"])
        ):
            raise ValueError("POSITIONING_COMPONENT_DIGEST_INVALID")
        if value["status"] == "OBSERVED" and value["sha256"] is None:
            raise ValueError("POSITIONING_COMPONENT_DIGEST_REQUIRED")
        if value["sha256"] is not None and value["sha256"] != digest(
            _json({"rows": value["rows"]})
        ):
            raise ValueError("POSITIONING_COMPONENT_DIGEST_MISMATCH")
    if (
        not isinstance(features, dict)
        or features.get("execution_authorized") is not False
        or features.get("automatic_parameter_updates") is not False
        or features.get("promotion_authorized", False) is not False
    ):
        raise ValueError("POSITIONING_RESEARCH_ONLY_REQUIRED")
    members = _membership(membership)
    record = {
        "bundle": bundle,
        "features": features,
        "membership": members,
        "slot_ms": slot_ms,
    }
    encoded = _json(record)
    return symbol, members, digest(encoded)


def _catalog_symbols(catalog, now_ms):
    if not isinstance(catalog, list) or len(catalog) > 5000:
        raise ValueError("POSITIONING_CATALOG_INVALID")
    eligible, seen = set(), set()
    for row in catalog:
        if not isinstance(row, dict):
            raise ValueError("POSITIONING_CATALOG_INVALID")  # noqa: TRY004 - stable research code
        if (
            row.get("status") != "TRADING"
            or row.get("contractType") != "PERPETUAL"
            or row.get("quoteAsset") != "USDT"
        ):
            continue
        symbol = _symbol(row.get("symbol"))
        if symbol in seen:
            raise ValueError("POSITIONING_CATALOG_DUPLICATE")
        seen.add(symbol)
        onboard, delivery = row.get("onboardDate"), row.get("deliveryDate")
        if (
            type(onboard) is int
            and type(delivery) is int
            and 0 <= onboard <= now_ms < delivery < 2**53
        ):
            eligible.add(symbol)
    return eligible


def _record(row):
    identity, slot, bundle, features, members, receipt, fingerprint = row
    _, normalized, checked = _prepared(bundle, slot, members, features)
    if (
        normalized != members
        or checked != fingerprint
        or type(receipt) is not int
        or receipt < bundle["finished_at_ms"]
    ):
        raise ValueError("POSITIONING_STORED_EVIDENCE_CORRUPT")
    return {
        "observation_id": str(identity),
        "slot_ms": slot,
        "bundle": bundle,
        "features": features,
        "membership": members,
        "first_received_at_ms": receipt,
        "input_digest": fingerprint,
    }


_COLUMNS = "observation_id,slot_ms,bundle,features,membership,first_received_at_ms,input_digest"


class PositioningStore:
    def __init__(self, connect):
        self.connect = connect

    def select_symbols(self, now_ms, limit=8):
        _ms(now_ms)
        if type(limit) is not int or not 1 <= limit <= 40:
            raise ValueError("POSITIONING_SELECTION_LIMIT")
        slot = now_ms // SLOT_MS * SLOT_MS
        civil_day = (now_ms + 8 * 3_600_000) // DAY_MS
        with self.connect() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout='3s'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            saved = conn.execute(
                "SELECT catalog,ranking FROM v2_daily_mover_days WHERE catalog_observed_at_ms BETWEEN %s AND %s "
                "ORDER BY catalog_observed_at_ms DESC,day DESC,method_version LIMIT 1",
                (max(0, now_ms - 3 * DAY_MS), now_ms),
            ).fetchone()
            if saved is None:
                return []
            eligible = _catalog_symbols(saved[0], now_ms)
            if not eligible:
                return []
            controls = set(
                sorted(
                    eligible,
                    key=lambda s: (
                        digest(
                            canonical(
                                {"version": VERSION, "day": civil_day, "symbol": s}
                            )
                        ),
                        s,
                    ),
                )[:20]
            )
            ranking = saved[1] or {}
            if not isinstance(ranking, dict):
                raise ValueError("POSITIONING_RANKING_INVALID")  # noqa: TRY004 - stable research code
            tops = set()
            for group in ("winners", "losers"):
                values = ranking.get(group, [])
                if not isinstance(values, list) or len(values) > 10:
                    raise ValueError("POSITIONING_RANKING_INVALID")
                for row in values:
                    if not isinstance(row, dict):
                        raise ValueError("POSITIONING_RANKING_INVALID")  # noqa: TRY004 - stable research code
                    symbol = _symbol(row.get("symbol"))
                    if symbol in eligible:
                        tops.add(symbol)
            # Only current bound SANDBOX accounts and actually filled residuals.
            # No account identifiers leave SQL or enter the research observation.
            active = conn.execute(
                """WITH positions AS MATERIALIZED (
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
                    AND i.payload->>'symbol'=ANY(%s)
                    AND (SELECT coalesce(sum(CASE WHEN o.leg='OPEN' THEN f.quantity ELSE -f.quantity END),0)
                      FROM v2_orders o JOIN v2_fills f USING(order_id) WHERE o.episode_id=i.intent_id)>0)
                SELECT p.symbol FROM positions p LEFT JOIN LATERAL (
                  SELECT slot_ms FROM v2_positioning_observations x
                  WHERE x.symbol=p.symbol AND x.source=%s AND x.method_version=%s
                  ORDER BY slot_ms DESC LIMIT 1) last ON true
                ORDER BY last.slot_ms NULLS FIRST,p.symbol LIMIT 40""",
                (sorted(eligible), SOURCE, VERSION),
            ).fetchall()
            active = {r[0] for r in active}
            candidates = controls | tops | active
            if len(candidates) > MAX_CANDIDATES:
                raise ValueError("POSITIONING_CANDIDATES_BOUND")
            latest = dict(
                conn.execute(
                    "SELECT candidate.symbol,last.slot_ms FROM unnest(%s::text[]) candidate(symbol) CROSS JOIN LATERAL ("
                    "SELECT slot_ms FROM v2_positioning_observations WHERE source=%s AND method_version=%s "
                    "AND symbol=candidate.symbol ORDER BY slot_ms DESC LIMIT 1) last",
                    (sorted(candidates), SOURCE, VERSION),
                ).fetchall()
            )
        available = {s for s in candidates if latest.get(s, -1) < slot}
        order = lambda s: (
            latest.get(s, -1),
            digest(canonical({"day": civil_day, "symbol": s})),
            s,
        )
        research = sorted((controls | tops) & available, key=order)
        active_lane = sorted(active & available, key=order)
        # Active inventory may use at most half when research is waiting.
        # A one-slot caller alternates lanes by clock, rather than starving one.
        active_quota = limit // 2 if limit > 1 else (slot // SLOT_MS) % 2
        chosen = active_lane[:active_quota] if research else active_lane[:limit]
        chosen.extend(s for s in research if s not in chosen)
        chosen = chosen[:limit]
        chosen.extend(s for s in active_lane if s not in chosen)
        result = []
        for symbol in chosen[:limit]:
            membership = []
            if symbol in controls:
                membership.append("FIXED_CONTROL")
            if symbol in tops:
                membership.append("TOP_MOVER_DIAGNOSTIC")
            if symbol in active:
                membership.append("ACTIVE_SANDBOX_POSITION")
            result.append({"symbol": symbol, "membership": sorted(membership)})
        return result

    def save(self, bundle, *, slot_ms, membership, features):
        symbol, membership, fingerprint = _prepared(
            bundle, slot_ms, membership, features
        )
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='3s'")
            identity = uuid4()
            inserted = conn.execute(
                "INSERT INTO v2_positioning_observations(observation_id,source,symbol,slot_ms,method_version,bundle,features,membership,input_digest) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(source,symbol,slot_ms,method_version) DO NOTHING RETURNING observation_id",
                (
                    identity,
                    SOURCE,
                    symbol,
                    slot_ms,
                    VERSION,
                    Jsonb(bundle),
                    Jsonb(features),
                    Jsonb(membership),
                    fingerprint,
                ),
            ).fetchone()
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM v2_positioning_observations WHERE source=%s AND symbol=%s AND slot_ms=%s AND method_version=%s",
                (SOURCE, symbol, slot_ms, VERSION),
            ).fetchone()
            if row is None:
                raise ValueError("POSITIONING_SAVE_MISSING")
            stored = _record(row)
            return {
                "observation_id": stored["observation_id"],
                "inserted": inserted is not None,
                "first_received_at_ms": stored["first_received_at_ms"],
                "same_input": stored["input_digest"] == fingerprint,
                "input_digest": stored["input_digest"],
            }

    def recent(self, symbol, *, before_ms, limit=24):
        _symbol(symbol)
        _ms(before_ms)
        if type(limit) is not int or not 1 <= limit <= 288:
            raise ValueError("POSITIONING_RECENT_LIMIT")
        with self.connect() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout='3s'")
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM v2_positioning_observations WHERE source=%s AND symbol=%s AND method_version=%s "
                "AND slot_ms<=%s AND first_received_at_ms<=%s ORDER BY slot_ms DESC LIMIT %s",
                (SOURCE, symbol, VERSION, before_ms, before_ms, limit),
            ).fetchall()
        return [_record(row) for row in rows]

    def record_run(
        self, *, started_at_ms, completed_at_ms, status, coverage, error_code=None
    ):
        _ms(started_at_ms)
        _ms(completed_at_ms)
        if (
            not started_at_ms <= completed_at_ms <= started_at_ms + 3_600_000
            or status not in RUN_STATUSES
        ):
            raise ValueError("POSITIONING_RUN_INVALID")
        if error_code is not None and (
            not isinstance(error_code, str)
            or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", error_code)
        ):
            raise ValueError("POSITIONING_RUN_ERROR_INVALID")
        _json(coverage, maximum=16_384)
        identity = uuid4()
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='3s'")
            conn.execute(
                "INSERT INTO v2_positioning_runs(run_id,method_version,started_at_ms,completed_at_ms,status,coverage,error_code) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                (
                    identity,
                    VERSION,
                    started_at_ms,
                    completed_at_ms,
                    status,
                    Jsonb(coverage),
                    error_code,
                ),
            )
        return str(identity)

    def recent_runs(self, *, before_ms, limit=24):
        _ms(before_ms)
        if type(limit) is not int or not 1 <= limit <= 288:
            raise ValueError("POSITIONING_RECENT_LIMIT")
        with self.connect() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET LOCAL statement_timeout='3s'")
            rows = conn.execute(
                "SELECT run_id::text,started_at_ms,completed_at_ms,status,coverage,error_code FROM v2_positioning_runs "
                "WHERE method_version=%s AND completed_at_ms<=%s AND created_at<=to_timestamp(%s/1000.0) "
                "ORDER BY completed_at_ms DESC,run_id LIMIT %s",
                (VERSION, before_ms, before_ms, limit),
            ).fetchall()
        keys = (
            "run_id",
            "started_at_ms",
            "completed_at_ms",
            "status",
            "coverage",
            "error_code",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def prune(self, now_ms, *, limit=500):
        _ms(now_ms)
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("POSITIONING_PRUNE_LIMIT")
        with self.connect() as conn:
            conn.execute("SET LOCAL statement_timeout='3s'")
            conn.execute("SET LOCAL lock_timeout='200ms'")
            # The dedicated job advisory lock normally serializes retention.
            # A bounded DELETE also tolerates a second caller without needing
            # UPDATE privilege; explicit FOR UPDATE would require that privilege.
            # Lock contention rolls this transaction back for the next run.
            return conn.execute(
                "WITH old AS (SELECT observation_id FROM v2_positioning_observations WHERE source=%s AND method_version=%s "
                "AND first_received_at_ms<%s ORDER BY first_received_at_ms,observation_id LIMIT %s) "
                "DELETE FROM v2_positioning_observations x USING old WHERE x.observation_id=old.observation_id",
                (SOURCE, VERSION, max(0, now_ms - 30 * DAY_MS), limit),
            ).rowcount
