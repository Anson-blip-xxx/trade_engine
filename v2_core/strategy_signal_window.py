"""Initial subscription boundary for explicitly enabled typed consumers.

An instance owns raw inputs which were still valid at its first immutable
deployment publication. It does not owe a new expiry audit for older inbox
history. Existing work is always independently discoverable. This is a work
selection boundary, never permission to submit or a proof of queue exhaustion.
"""

from dataclasses import asdict

from v2_core.account_risk import AccountScope
from v2_core.strategy_deployment import _key, _read_current, _restore, _stored_payload


def initial_signal_cutoff(conn, scope):
    account = AccountScope(
        **{name: getattr(scope, name) for name in AccountScope.__dataclass_fields__}
    )
    key = _key(account, scope.producer)
    if _read_current(conn, key) is None:
        raise ValueError("STRATEGY_SIGNAL_WINDOW_DEPLOYMENT_REQUIRED")
    row = conn.execute(
        "SELECT payload,deleted,expected_version,reason,"
        "floor(extract(epoch FROM created_at)*1000)::bigint,"
        "floor(extract(epoch FROM clock_timestamp())*1000)::bigint "
        "FROM v2_state_history WHERE state_id=%s AND version=1",
        (key.identity,),
    ).fetchone()
    try:
        first = _restore(row[0]["binding"])
        if (
            first.scope != account
            or first.instance_id != scope.producer
            or first.generation != 1
            or row[:4]
            != (_stored_payload(first), False, 0, "STRATEGY_DEPLOYMENT_PUBLISHED")
            or type(row[4]) is not int
            or not 0 <= row[4] <= row[5]
        ):
            raise ValueError("invalid first publication")
    except (TypeError, ValueError, KeyError, IndexError):
        raise ValueError("STRATEGY_SIGNAL_WINDOW_HISTORY_INVALID") from None
    return row[4]


# Materialize the indexed expiry range before joining/sorting request keys.
# The null branch preserves malformed missing-expiry inputs for visible failure,
# rather than falsely claiming that a corrupt signal predates deployment.
# Recovery unions do not impose the raw-input cutoff or filter intent status.
_PAGE_SQL = """WITH raw AS MATERIALIZED (
    SELECT signal_id,request_key FROM v2_inbound_signals
    WHERE source=%s AND environment=%s
    AND ((snapshot->>'expires_at_ms')::bigint>%s
        OR (snapshot->>'expires_at_ms')::bigint IS NULL)),
recovery AS MATERIALIZED (
    SELECT signal_id FROM v2_strategy_entry_candidates WHERE consumer=%s
    UNION SELECT signal_id FROM v2_signal_receipts WHERE consumer=%s
    UNION SELECT signal_id FROM v2_strategy_tasks WHERE consumer=%s
    UNION SELECT signal_id FROM v2_strategy_decisions WHERE consumer=%s
    UNION SELECT signal_id FROM v2_trade_intents
    WHERE (exchange,account_id,environment,product,producer)=(%s,%s,%s,%s,%s)
        AND signal_id IS NOT NULL),
eligible AS (
    SELECT signal_id,request_key FROM raw
    UNION SELECT s.signal_id,s.request_key FROM recovery r
    JOIN v2_inbound_signals s USING(signal_id)
    WHERE s.source=%s AND s.environment=%s)
SELECT signal_id::text,request_key FROM eligible
WHERE (%s::text IS NULL OR request_key>%s) ORDER BY request_key LIMIT %s"""


def deployment_history_page(conn, *, scope, source, cursor, size):
    cutoff = initial_signal_cutoff(conn, scope)
    return conn.execute(
        _PAGE_SQL,
        (
            source,
            scope.environment,
            cutoff,
            scope.consumer,
            scope.consumer,
            scope.consumer,
            scope.consumer,
            *asdict(scope).values(),
            source,
            scope.environment,
            cursor,
            cursor,
            size,
        ),
    ).fetchall()


def has_consumer_work(conn, scope, signal_id):
    """The cutoff may never dispose of a previously scheduled recovery item."""
    return conn.execute(
        "SELECT EXISTS(SELECT 1 FROM v2_strategy_tasks WHERE consumer=%s AND signal_id=%s) "
        "OR EXISTS(SELECT 1 FROM v2_strategy_decisions WHERE consumer=%s AND signal_id=%s) "
        "OR EXISTS(SELECT 1 FROM v2_trade_intents WHERE signal_id=%s AND "
        "(exchange,account_id,environment,product,producer)=(%s,%s,%s,%s,%s))",
        (
            scope.consumer,
            signal_id,
            scope.consumer,
            signal_id,
            signal_id,
            *asdict(scope).values(),
        ),
    ).fetchone()[0]
