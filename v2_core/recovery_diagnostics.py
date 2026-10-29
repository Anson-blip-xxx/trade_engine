"""Bounded query diagnostics, separate from authoritative order transitions.

Identical observations are deduplicated. The history row's database timestamp
is the first observation of that diagnostic, not a heartbeat; recovery leases
retain the polling schedule. No response body, credential or arbitrary error
message belongs in this namespace.
"""

from contextlib import nullcontext
from dataclasses import asdict

from v2_core.evidence import canonical
from v2_core.ledger import lock_order_episode
from v2_core.state import BusinessState, StateKey
from v2_core.transport import ExchangeTransportError

NAMESPACE = "order-recovery-diagnostic-v1"


def transport_evidence(exc):
    """Do not execute a subclass's arbitrary diagnostic exporter."""
    if not isinstance(exc, ExchangeTransportError):
        return {"category": "QUERY_OR_VALIDATION_ERROR"}
    try:
        return ExchangeTransportError.diagnostic_evidence(exc)
    except Exception:  # noqa: BLE001 - diagnostic formatting cannot mask the query error
        return {"category": "TRANSPORT_ERROR"}


def _diagnostic(value):
    if not isinstance(value, dict):
        raise ValueError("INVALID_RECOVERY_DIAGNOSTIC")  # noqa: TRY004 - protocol validation
    outcome = value.get("outcome")
    if outcome in ("BOUND_ORDER_LOOKUPS_ABSENT", "BOUND_ORDER_LOOKUP_FOUND"):
        if set(value) != {"outcome", "query"}:
            raise ValueError("INVALID_RECOVERY_DIAGNOSTIC")
        query_proof(value["query"])
    elif outcome in ("NO_OBSERVATION", "QUERY_OBSERVED"):
        if set(value) != {"outcome"}:
            raise ValueError("INVALID_RECOVERY_DIAGNOSTIC")
    elif outcome == "QUERY_FAILED":
        if set(value) != {"outcome", "transport"}:
            raise ValueError("INVALID_RECOVERY_DIAGNOSTIC")
        transport = value["transport"]
        if (
            not isinstance(transport, dict)
            or not set(transport) <= {"category", "http_status", "exchange_code"}
            or transport.get("category")
            not in (
                "EXCHANGE_RESPONSE_ERROR",
                "NETWORK_OUTCOME_UNKNOWN",
                "INVALID_RESPONSE",
                "RESPONSE_TOO_LARGE",
                "ENDPOINT_OR_WRITE_DISABLED",
                "QUOTA_DENIED",
                "TRANSPORT_ERROR",
                "QUERY_OR_VALIDATION_ERROR",
            )
            or (
                "http_status" in transport
                and (
                    type(transport["http_status"]) is not int
                    or not 100 <= transport["http_status"] <= 599
                )
            )
            or (
                "exchange_code" in transport
                and (
                    type(transport["exchange_code"]) is not int
                    or not -100000 <= transport["exchange_code"] <= 100000
                )
            )
        ):
            raise ValueError("INVALID_RECOVERY_DIAGNOSTIC")
    else:
        raise ValueError("INVALID_RECOVERY_DIAGNOSTIC")
    return value


def query_proof(value):
    """Accept only the adapter's two bounded lookup results, never extra text."""
    if (
        not isinstance(value, dict)
        or set(value) != {"bound_exchange_order_id", "attempts"}
        or value["bound_exchange_order_id"] is not True
        or not isinstance(value["attempts"], list)
        or len(value["attempts"]) != 2
    ):
        raise ValueError("INVALID_RECOVERY_QUERY_DIAGNOSTIC")
    for index, attempt in enumerate(value["attempts"]):
        if (
            not isinstance(attempt, dict)
            or set(attempt) != {"method", "outcome", "transport"}
            or attempt["method"] != ("CLIENT_ORDER_ID", "EXCHANGE_ORDER_ID")[index]
            or attempt["outcome"] not in {"ABSENT", "FOUND"}
            or (index == 0 and attempt["outcome"] != "ABSENT")
        ):
            raise ValueError("INVALID_RECOVERY_QUERY_DIAGNOSTIC")
        transport = attempt["transport"]
        if attempt["outcome"] == "FOUND":
            if transport != {}:
                raise ValueError("INVALID_RECOVERY_QUERY_DIAGNOSTIC")
        elif (
            not isinstance(transport, dict)
            or set(transport)
            not in (
                {"category", "exchange_code"},
                {"category", "exchange_code", "http_status"},
            )
            or transport.get("category") != "EXCHANGE_RESPONSE_ERROR"
            or type(transport.get("exchange_code")) is not int
            or transport["exchange_code"] != -2013
            or (
                "http_status" in transport
                and (
                    type(transport["http_status"]) is not int
                    or transport["http_status"] != 400
                )
            )
        ):
            raise ValueError("INVALID_RECOVERY_QUERY_DIAGNOSTIC")
    return value


def record(connect, current, diagnostic):
    """Serialize by the same episode lock as fills; ignore stale query results."""
    key = StateKey(
        **{
            name: current[name]
            for name in ("exchange", "account_id", "environment", "product")
        },
        namespace=NAMESPACE,
        key=current["order_id"],
    )
    payload = {
        "order_version": current["version"],
        "local_status": current["status"],
        "bound_exchange_order_id": current["exchange_order_id"] is not None,
        **_diagnostic(diagnostic),
    }
    encoded = canonical(payload)
    with connect() as conn:
        # This is not the order journal: never spend the normal trading lock
        # budget waiting to persist an auxiliary diagnostic.
        conn.execute("SET LOCAL lock_timeout='100ms'")
        conn.execute("SET LOCAL statement_timeout='500ms'")
        lock_order_episode(conn, current["order_id"])
        row = conn.execute(
            "SELECT version,status FROM v2_orders WHERE order_id=%s",
            (current["order_id"],),
        ).fetchone()
        if row != (current["version"], current["status"]):
            return "STALE"
        state = BusinessState(lambda: nullcontext(conn))
        previous = state.read(key)
        if diagnostic["outcome"] == "QUERY_OBSERVED" and (
            previous is None or previous.deleted
        ):
            # Successful normal queries supersede an earlier error, but are
            # not a second journal of every routinely observed order.
            return "NO_DIAGNOSTIC"
        if previous and not previous.deleted and previous.payload_json == encoded:
            return "UNCHANGED"
        version = previous.version if previous else 0
        return state.change(
            key,
            expected_version=version,
            request_key=f"query:{version + 1}",
            payload=payload,
            reason="ORDER_RECOVERY_QUERY_OBSERVED",
        ).code


def summaries(conn, scope, orders):
    """Bounded primary-key reads for health; timestamps are not last-poll times."""
    if len(orders) > 20:
        raise ValueError("RECOVERY_DIAGNOSTIC_READ_BOUND")
    keys = {}
    for oid, version in orders:
        key = StateKey(**asdict(scope), namespace=NAMESPACE, key=oid)
        keys[key.identity] = (oid, version, asdict(key))
    if not keys:
        return {}
    try:
        with conn.transaction():
            # A savepoint restores the caller's transaction and timeout settings
            # even on SQL failure. Optional detail cannot disable health checks.
            original = conn.execute(
                "SELECT current_setting('lock_timeout'),current_setting('statement_timeout')"
            ).fetchone()
            conn.execute("SET LOCAL lock_timeout='100ms'")
            conn.execute("SET LOCAL statement_timeout='500ms'")
            rows = conn.execute(
                """SELECT s.state_id::text,s.payload,
                (extract(epoch FROM h.created_at)*1000)::bigint,s.scope
                FROM v2_business_state s JOIN v2_state_history h
                  ON h.state_id=s.state_id AND h.version=s.version
                WHERE s.state_id=ANY(%s::uuid[]) AND NOT s.deleted""",
                (list(keys),),
            ).fetchall()
            conn.execute(
                "SELECT set_config('lock_timeout',%s,true),set_config('statement_timeout',%s,true)",
                original,
            )
    except Exception:  # noqa: BLE001 - bounded auxiliary diagnostics must fail soft
        return {oid: {"evidence_status": "UNAVAILABLE"} for oid, _ in orders}
    result = {}
    for identity, payload, timestamp, saved_scope in rows:
        oid, version, expected_scope = keys[identity]
        if (
            saved_scope != expected_scope
            or not isinstance(payload, dict)
            or type(payload.get("order_version")) is not int
            or payload.get("order_version") != version
            or not isinstance(payload.get("outcome"), str)
            or payload.get("outcome")
            not in {
                "NO_OBSERVATION",
                "QUERY_OBSERVED",
                "QUERY_FAILED",
                "BOUND_ORDER_LOOKUPS_ABSENT",
                "BOUND_ORDER_LOOKUP_FOUND",
            }
        ):
            continue
        result[oid] = {
            "outcome": payload["outcome"],
            "first_observed_at_ms": timestamp,
            "timestamp_basis": "DIAGNOSTIC_CHANGE_NOT_LAST_POLL",
        }
        if "query" in payload:
            try:
                result[oid]["query"] = query_proof(payload["query"])
            except (TypeError, ValueError, KeyError):
                result[oid]["evidence_status"] = "INVALID_DIAGNOSTIC"
        # Never send an arbitrary stored payload or transport error message to UI.
    return result
