"""Durable, bounded JEV advisory coordination; no SDK, credentials or orders.

Each public request belongs to an explicit internal scope. All scopes share one
provider budget, including LIVE/SANDBOX; account aliases cannot mint new quotas.
Only an owned claim may publish. Ambiguous/expired claims are never reclaimed:
the upstream call may have consumed tokens even when its response was lost.
"""

import json
import re
from contextlib import nullcontext
from dataclasses import asdict
from uuid import UUID, uuid4

from v2_core.account_risk import AccountScope
from v2_core.jev_advisory import (
    RESERVE_TOKENS,
    AdvisoryPolicy,
    _cached,
    _integer,
    _result,
    prepare_request,
)
from v2_core.state import BusinessState, StateKey

BUDGET_KEY = "shared-jev-primary"
CONTROL_KEY = StateKey(
    "TYPESAFE", BUDGET_KEY, "SANDBOX", "RESEARCH", "jev-advisory-control-v1", "current"
)
REQUEST_NAMESPACE = "jev-advisory-request-v1"
MINUTE_NAMESPACE = "jev-advisory-minute-budget-v1"
DAY_NAMESPACE = "jev-advisory-day-budget-v1"
_LOCK = "trade-v2:jev-advisory:" + BUDGET_KEY
_SCHEMA = "jev-pg-coordinator-v1"
_FALLBACK_REASONS = {
    "MODEL_UNAVAILABLE",
    "MODEL_RESPONSE_INVALID",
    "MODEL_UNKNOWN_FAILURE",
    "MODEL_LATE_RESPONSE",
    "PROVIDER_REQUEST_REJECTED",
    "MODEL_CONFIGURATION_UNAVAILABLE",
    "MODEL_TIMEOUT",
    "TOKEN_RESERVATION_EXCEEDED",
    "DEADLINE_EXPIRED",
}


def _key(value):
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("JEV_REQUEST_KEY_INVALID")
    return value


def _clock(conn):
    return _integer(
        conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp()) * 1000)::bigint"
        ).fetchone()[0],
        0,
        2**53 - 1,
    )


def _limit_transaction(conn):
    # This connection belongs only to the background advisory operation. Never
    # inherit execution SQL limits or alter a borrowed trading transaction.
    conn.execute("SET LOCAL lock_timeout='50ms'")
    conn.execute("SET LOCAL statement_timeout='100ms'")


def _window_starts(now):
    """Minute and Asia/Shanghai calendar day, independent of SQL session TZ."""
    return now // 60000 * 60000, (now + 28800000) // 86400000 * 86400000 - 28800000


def _budget_key(namespace, start):
    return StateKey("JEV", BUDGET_KEY, "GLOBAL", "ADVISORY", namespace, str(start))


def _read(store, key):
    saved = store.read(key)
    if saved is None:
        return 0, None
    if saved.deleted:
        # A retention tombstone is not permission to call the provider again.
        raise ValueError("JEV_STATE_TOMBSTONED")
    return saved.version, json.loads(saved.payload_json)


def _write(store, key, version, payload, operation):
    written = store.change(
        key,
        expected_version=version,
        request_key=operation,
        payload=payload,
        reason="JEV durable advisory coordination",
    )
    if written.code != "APPLIED":
        # Raising rolls back *all* request and budget writes in this transaction.
        raise ValueError("JEV_COORDINATOR_CAS_CONFLICT")


def _budget(payload, *, kind, start, limit):
    if payload is None:
        return {
            "schema": _SCHEMA,
            "budget_key": BUDGET_KEY,
            "kind": kind,
            "window_start_ms": start,
            "used": 0,
            "limit": limit,
        }
    if (
        type(payload) is not dict
        or set(payload)
        != {"schema", "budget_key", "kind", "window_start_ms", "used", "limit"}
        or payload["schema"] != _SCHEMA
        or payload["budget_key"] != BUDGET_KEY
        or payload["kind"] != kind
        or payload["window_start_ms"] != start
    ):
        raise ValueError("JEV_BUDGET_INVALID")
    _integer(payload["used"], 0, 2**53 - 1)
    _integer(payload["limit"], 1, 1000000)
    # Different accounts/processes cannot relax an already lower shared limit.
    return {**payload, "limit": min(payload["limit"], limit)}


def _token_budget_reason(value, *, now, pacing=False, burst_tokens=16384):
    """Same read-only admission test for preflight and atomic claiming.

    Burst includes the full reservation; it is not added to the daily limit.
    Pure elapsed-time pacing uses the existing day row unchanged. The claiming
    transaction reads the shared control, so account/process flags cannot relax
    its pacing; old code can still read the original budget row on rollback.
    """
    _integer(now, 0, 2**53 - 1)
    if type(pacing) is not bool:
        raise ValueError("JEV_EXPLICIT_PACING_SWITCH_REQUIRED")
    _integer(burst_tokens, RESERVE_TOKENS, 131072)
    if value["window_start_ms"] != _window_starts(now)[1]:
        raise ValueError("JEV_BUDGET_WINDOW_INVALID")
    if value["used"] + RESERVE_TOKENS > value["limit"]:
        return "TOKEN_BUDGET_EXHAUSTED"
    if pacing:
        burst = min(value["limit"], burst_tokens)
        elapsed = now - value["window_start_ms"]
        released = burst + (value["limit"] - burst) * elapsed // 86400000
        if value["used"] + RESERVE_TOKENS > released:
            return "TOKEN_BUDGET_PACED"
    return None


class JevCoordinator:
    """PostgreSQL implementation of the three JevAdvisory coordinator ports.

    ``connect`` must yield a fresh, dedicated psycopg connection, never a borrowed
    trading connection; explicit transactions also work with autocommit. Each
    operation sets 50ms lock/100ms SQL timeouts. Connection setup may still use
    the factory's 5s bound, only in the background worker. The model's <=2s RPC
    deadline is not an end-to-end database latency guarantee. No network calls
    occur under the global lock.
    Limits here are shared hard caps; call parameters may only reduce them.
    ``now_ms`` is checked for shape/causality but never selects budget windows.
    Lookup uses DB time and is read-only. Default OFF does not even open a DB.
    """

    def __init__(
        self,
        connect,
        *,
        scope,
        enabled=False,
        requests_per_minute=5,
        tokens_per_day=500000,
        max_age_ms=60000,
        cache_ttl_ms=30000,
        token_budget_pacing=False,
        token_pacing_burst_tokens=16384,
    ):
        if not callable(connect) or not isinstance(scope, AccountScope):
            raise TypeError("JEV_EXPLICIT_DATABASE_AND_SCOPE_REQUIRED")
        if type(enabled) is not bool:
            raise ValueError("JEV_EXPLICIT_SWITCH_REQUIRED")
        self.connect, self.scope, self.enabled = connect, scope, enabled
        self.requests_per_minute = _integer(requests_per_minute, 1, 60)
        self.tokens_per_day = _integer(tokens_per_day, RESERVE_TOKENS, 1000000)
        self.max_age_ms = _integer(max_age_ms, 1000, 300000)
        self.cache_ttl_ms = _integer(cache_ttl_ms, 1000, self.max_age_ms)
        if type(token_budget_pacing) is not bool:
            raise ValueError("JEV_EXPLICIT_PACING_SWITCH_REQUIRED")
        self.token_budget_pacing = token_budget_pacing
        self.token_pacing_burst_tokens = _integer(
            token_pacing_burst_tokens, RESERVE_TOKENS, 131072
        )

    def request_state_key(self, request_key):
        return StateKey(
            **asdict(self.scope), namespace=REQUEST_NAMESPACE, key=_key(request_key)
        )

    def _prepare(self, request_key, request, now):
        if type(request) is not dict or set(request) != {"model", "state", "questions"}:
            raise ValueError("JEV_REQUEST_INVALID")
        prepared = prepare_request(
            request["state"], now_ms=now, max_age_ms=self.max_age_ms
        )
        if prepared["request"] != request or prepared["request_key"] != _key(
            request_key
        ):
            raise ValueError("JEV_REQUEST_IDENTITY_MISMATCH")
        return prepared

    def _cache(self, payload, request_key, now):
        if (
            type(payload) is not dict
            or payload.get("schema") != _SCHEMA
            or payload.get("status") != "COMPLETED"
        ):
            return None
        result = payload.get("result")
        if type(result) is not dict or result.get("status") != "ADVISORY_READY":
            return None
        try:
            prepared = self._prepare(request_key, payload["request"], now)
            oldest_observed = prepared["valid_until_ms"] - self.max_age_ms
            prepared["valid_until_ms"] = min(
                prepared["valid_until_ms"],
                _integer(
                    payload["valid_until_ms"],
                    oldest_observed + 1000,
                    oldest_observed + 300000,
                ),
            )
            return _cached(result, prepared, now, self.cache_ttl_ms)
        except (KeyError, TypeError, ValueError):
            return None

    def lookup(self, *, request_key, now_ms):
        if not self.enabled:
            return None
        _integer(now_ms, 0, 2**53 - 1)
        key = self.request_state_key(request_key)
        with self.connect() as conn, conn.transaction():
            _limit_transaction(conn)
            now = _clock(conn)
            _, payload = _read(BusinessState(lambda: nullcontext(conn)), key)
            return self._cache(payload, request_key, now)

    def claim(
        self,
        *,
        request_key,
        request,
        now_ms,
        deadline_ms,
        reserve_tokens=RESERVE_TOKENS,
        requests_per_minute=5,
        tokens_per_day=500000,
        token_budget_pacing=False,
        token_pacing_burst_tokens=16384,
    ):
        if not self.enabled:
            return {"status": "BUSY"}
        _integer(now_ms, 0, 2**53 - 1)
        _integer(deadline_ms, now_ms + 1, now_ms + 2000)
        if type(reserve_tokens) is not int or reserve_tokens != RESERVE_TOKENS:
            raise ValueError("JEV_FULL_TOKEN_RESERVATION_REQUIRED")
        rpm = min(self.requests_per_minute, _integer(requests_per_minute, 1, 60))
        tpd = min(
            self.tokens_per_day, _integer(tokens_per_day, RESERVE_TOKENS, 1000000)
        )
        if type(token_budget_pacing) is not bool:
            raise ValueError("JEV_EXPLICIT_PACING_SWITCH_REQUIRED")
        _integer(token_pacing_burst_tokens, RESERVE_TOKENS, 131072)
        pacing = self.token_budget_pacing or token_budget_pacing
        bursts = [
            value
            for active, value in (
                (self.token_budget_pacing, self.token_pacing_burst_tokens),
                (token_budget_pacing, token_pacing_burst_tokens),
            )
            if active
        ]
        burst_tokens = min(bursts) if bursts else self.token_pacing_burst_tokens
        key = self.request_state_key(request_key)
        with self.connect() as conn, conn.transaction():
            _limit_transaction(conn)
            if not conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", (_LOCK,)
            ).fetchone()[0]:
                return {"status": "BUSY"}
            now = _clock(conn)
            prepared = self._prepare(request_key, request, now)
            if not now < deadline_ms <= min(now + 2000, prepared["valid_until_ms"]):
                return {"status": "BUSY"}
            store = BusinessState(lambda: nullcontext(conn))
            version, current = _read(store, key)
            if current is not None:
                cached = self._cache(current, request_key, now)
                return (
                    {"status": "CACHED", "result": cached}
                    if cached is not None
                    else {"status": "BUSY"}
                )
            # Read the existing shared policy under the same global lock as
            # reservation. Explicit offline coordinators may omit this row.
            _, control_payload = _read(store, CONTROL_KEY)
            if control_payload is not None:
                control = AdvisoryPolicy(**control_payload)
                if not control.enabled:
                    return {"status": "BUSY"}
                rpm = min(rpm, control.requests_per_minute)
                tpd = min(tpd, control.tokens_per_day)
                if control.token_budget_pacing:
                    burst_tokens = (
                        min(burst_tokens, control.token_pacing_burst_tokens)
                        if pacing
                        else control.token_pacing_burst_tokens
                    )
                    pacing = True
            minute, day = _window_starts(now)
            minute_key, day_key = (
                _budget_key(MINUTE_NAMESPACE, minute),
                _budget_key(DAY_NAMESPACE, day),
            )
            mv, raw_minute = _read(store, minute_key)
            dv, raw_day = _read(store, day_key)
            minutes = _budget(raw_minute, kind="minute", start=minute, limit=rpm)
            days = _budget(raw_day, kind="utc8-day", start=day, limit=tpd)
            if minutes["used"] >= minutes["limit"]:
                return {"status": "BUDGET_EXHAUSTED"}
            token_reason = _token_budget_reason(
                days, now=now, pacing=pacing, burst_tokens=burst_tokens
            )
            if token_reason:
                return {
                    "status": token_reason
                    if token_reason == "TOKEN_BUDGET_PACED"
                    else "BUDGET_EXHAUSTED"
                }
            claim_id = str(uuid4())
            operation = "claim:" + claim_id
            _write(
                store,
                minute_key,
                mv,
                {**minutes, "used": minutes["used"] + 1},
                operation,
            )
            _write(
                store,
                day_key,
                dv,
                {**days, "used": days["used"] + reserve_tokens},
                operation,
            )
            _write(
                store,
                key,
                version,
                {
                    "schema": _SCHEMA,
                    "status": "CLAIMED",
                    "claim_id": claim_id,
                    "request": prepared["request"],
                    "claimed_at_ms": now,
                    "deadline_ms": deadline_ms,
                    "valid_until_ms": prepared["valid_until_ms"],
                    "budget_day_start_ms": day,
                    "reserved_input_tokens": reserve_tokens,
                },
                operation,
            )
            return {"status": "GRANTED", "claim_id": claim_id}

    def complete(
        self,
        *,
        request_key,
        claim_id,
        result,
        now_ms,
        expires_at_ms,
        charged_input_tokens=None,
    ):
        if not self.enabled:
            return False
        _integer(now_ms, 0, 2**53 - 1)
        _integer(expires_at_ms, 0, 2**53 - 1)
        if type(claim_id) is not str or str(UUID(claim_id)) != claim_id:
            raise ValueError("JEV_CLAIM_ID_INVALID")
        if charged_input_tokens is not None:
            _integer(charged_input_tokens, 1, 1000000)
        key = self.request_state_key(request_key)
        with self.connect() as conn, conn.transaction():
            _limit_transaction(conn)
            if not conn.execute(
                "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", (_LOCK,)
            ).fetchone()[0]:
                return False
            now = _clock(conn)
            store = BusinessState(lambda: nullcontext(conn))
            version, current = _read(store, key)
            if (
                current is None
                or current.get("schema") != _SCHEMA
                or current.get("status") != "CLAIMED"
                or current.get("claim_id") != claim_id
            ):
                return False
            # Validate the original snapshot at its DB claim time, permitting a
            # late FALLBACK to close a claim without inventing a fresh snapshot.
            prepared = self._prepare(
                request_key, current["request"], current["claimed_at_ms"]
            )
            oldest_observed = prepared["valid_until_ms"] - self.max_age_ms
            prepared["valid_until_ms"] = min(
                prepared["valid_until_ms"],
                _integer(
                    current["valid_until_ms"],
                    oldest_observed + 1000,
                    oldest_observed + 300000,
                ),
            )
            _integer(
                current["deadline_ms"],
                current["claimed_at_ms"] + 1,
                min(current["claimed_at_ms"] + 2000, prepared["valid_until_ms"]),
            )
            if (
                type(result) is not dict
                or result.get("produced_at_ms") != now_ms
                or result.get("expires_at_ms") != expires_at_ms
                or not current["claimed_at_ms"] <= now_ms <= now
            ):
                raise ValueError("JEV_COMPLETION_INVALID")
            if result.get("status") == "ADVISORY_READY":
                checked = _cached(result, prepared, now, self.cache_ttl_ms)
                if (
                    now_ms >= current["deadline_ms"]
                    or charged_input_tokens != checked["usage"]["input_tokens"]
                ):
                    raise ValueError("JEV_COMPLETION_INVALID")
            else:
                reason = result.get("reason")
                expected_expiry = max(
                    now_ms, min(prepared["valid_until_ms"], now_ms + self.cache_ttl_ms)
                )
                if (
                    reason not in _FALLBACK_REASONS
                    or expires_at_ms != expected_expiry
                    or result.get("execution_authorized") is not False
                    or result.get("risk_override") is not False
                    or (
                        reason
                        in {
                            "MODEL_TIMEOUT",
                            "MODEL_UNAVAILABLE",
                            "MODEL_RESPONSE_INVALID",
                            "MODEL_UNKNOWN_FAILURE",
                            "PROVIDER_REQUEST_REJECTED",
                            "MODEL_CONFIGURATION_UNAVAILABLE",
                        }
                        and charged_input_tokens is not None
                    )
                    or (
                        reason == "TOKEN_RESERVATION_EXCEEDED"
                        and (
                            charged_input_tokens is None
                            or charged_input_tokens <= RESERVE_TOKENS
                        )
                    )
                    or (
                        reason == "MODEL_LATE_RESPONSE"
                        and (
                            charged_input_tokens is None
                            or not 1 <= charged_input_tokens <= RESERVE_TOKENS
                        )
                    )
                    or (
                        reason
                        not in {
                            "MODEL_RESPONSE_INVALID",
                            "MODEL_UNKNOWN_FAILURE",
                            "TOKEN_RESERVATION_EXCEEDED",
                            "PROVIDER_REQUEST_REJECTED",
                            "MODEL_CONFIGURATION_UNAVAILABLE",
                        }
                        and (reason in {"DEADLINE_EXPIRED", "MODEL_LATE_RESPONSE"})
                        != (now_ms >= current["deadline_ms"])
                    )
                ):
                    raise ValueError("JEV_COMPLETION_INVALID")
                checked = _result(
                    "FALLBACK",
                    result["reason"],
                    prepared,
                    now_ms=now_ms,
                    expires_at_ms=expires_at_ms,
                )
                if result != checked:
                    raise ValueError("JEV_COMPLETION_INVALID")
            day = current["budget_day_start_ms"]
            if (
                type(day) is not int
                or day != _window_starts(current["claimed_at_ms"])[1]
            ):
                raise ValueError("JEV_BUDGET_WINDOW_INVALID")
            day_key = _budget_key(DAY_NAMESPACE, day)
            dv, raw_day = _read(store, day_key)
            if raw_day is None:
                raise ValueError("JEV_BUDGET_MISSING")
            days = _budget(
                raw_day, kind="utc8-day", start=day, limit=self.tokens_per_day
            )
            reserve = _integer(
                current["reserved_input_tokens"], RESERVE_TOKENS, RESERVE_TOKENS
            )
            charge = reserve if charged_input_tokens is None else charged_input_tokens
            if days["used"] < reserve:
                raise ValueError("JEV_BUDGET_RESERVATION_MISSING")
            operation = "complete:" + claim_id
            if charge != reserve:
                _write(
                    store,
                    day_key,
                    dv,
                    {**days, "used": days["used"] - reserve + charge},
                    operation,
                )
            _write(
                store,
                key,
                version,
                {
                    **current,
                    "status": "COMPLETED",
                    "completed_at_ms": now,
                    "charged_input_tokens": charged_input_tokens,
                    "result": checked,
                },
                operation,
            )
            return True
