"""Initial subscription boundary for explicitly enabled typed consumers.

An instance owns raw inputs which were still valid at its first immutable
deployment publication. It does not owe a new expiry audit for older inbox
history. Existing work is always independently discoverable. This is a work
selection boundary, never permission to submit or a proof of queue exhaustion.
"""

import json
import time
from dataclasses import asdict
from hashlib import sha256
from uuid import UUID

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


_PREFIX = "typed-history-v2:"
_LANES = (
    "raw",
    "missing_expiry",
    "v2_strategy_entry_candidates",
    "v2_signal_receipts",
    "v2_strategy_tasks",
    "v2_strategy_decisions",
    "v2_trade_intents",
)
_EXPIRY = "((snapshot->>'expires_at_ms')::bigint)"
_ALL_DONE = (1 << len(_LANES)) - 1


class HistoryPage(list):
    """Matching rows plus independent, bounded physical scan progress.

    A short/empty matching page is NOT exhaustion: a recovery lane can contain
    another source's work. Checkpoints are disposable seek hints, not receipts.
    They permit maintenance to checkpoint before each potentially failing item.
    """

    def __init__(self, rows, *, next_cursor, scanned, checkpoints):
        super().__init__(rows)
        self.next_cursor = next_cursor
        self.scanned = scanned
        self.checkpoints = checkpoints


def history_progress(page, size, started):
    if type(page) is not HistoryPage:
        raise ValueError("STRATEGY_HISTORY_PROGRESS_REQUIRED")
    return page.next_cursor, {
        "history_scanned": page.scanned,
        "history_page_size": size,
        "history_sweep_complete": page.next_cursor is None,
        "history_cursor_present": page.next_cursor is not None,
        "history_elapsed_ms": round((time.perf_counter() - started) * 1000),
    }


def _encode(state):
    return _PREFIX + json.dumps(state, separators=(",", ":"))


def _state(cursor, *, scope, source, cutoff):
    stamp = sha256(
        (scope.consumer + ":" + source + ":" + str(cutoff)).encode()
    ).hexdigest()[:24]
    empty = [stamp, 0, 0, [None] * len(_LANES)]
    # The old cursor ordered request_key, not expiry/UUID. Replaying safely is
    # the only valid migration; translating that key could omit original work.
    if not isinstance(cursor, str) or not cursor.startswith(_PREFIX):
        return empty
    try:
        state = json.loads(cursor[len(_PREFIX) :])
        if (
            type(state) is not list
            or len(state) != 4
            or state[0] != stamp
            or type(state[1]) is not int
            or not 0 <= state[1] < len(_LANES)
            or type(state[2]) is not int
            or not 0 <= state[2] < _ALL_DONE
            or type(state[3]) is not list
            or len(state[3]) != len(_LANES)
        ):
            return empty
        for lane, position in enumerate(state[3]):
            if position is None:
                continue
            if lane == 0:
                if (
                    type(position) is not list
                    or len(position) != 2
                    or type(position[0]) is not int
                    or not cutoff < position[0] < 2**63
                ):
                    return empty
                identity = position[1]
            else:
                identity = position
            if type(identity) is not str or str(UUID(identity)) != identity:
                return empty
        return state
    except (ValueError, TypeError, OverflowError, RecursionError):
        return empty


def _lane_page(conn, *, scope, source, cutoff, lane, position, size):
    """Limit at the indexed owner table BEFORE source matching/joins.

    Raw lanes seek expiry + UUID, so neither a large pre-deployment inbox nor
    accumulated post-deployment history is sorted/materialized on each page.
    Recovery is independent of expiry, intent status and raw subscription time.
    """
    if lane == 0:
        seek = f"AND ({_EXPIRY},signal_id)>(%s,%s::uuid)" if position else ""
        return conn.execute(
            f"SELECT signal_id::text,request_key,{_EXPIRY} FROM v2_inbound_signals "
            f"WHERE source=%s AND environment=%s AND {_EXPIRY}>%s {seek} "
            f"ORDER BY {_EXPIRY},v2_inbound_signals.signal_id LIMIT %s",
            (
                source,
                scope.environment,
                cutoff,
                *((position[0], position[1]) if position else ()),
                size,
            ),
        ).fetchall()
    seek = "AND signal_id>%s::uuid" if position else ""
    if lane == 1:
        return conn.execute(
            "SELECT signal_id::text,request_key FROM v2_inbound_signals "
            f"WHERE source=%s AND environment=%s AND {_EXPIRY} IS NULL {seek} "
            "ORDER BY v2_inbound_signals.signal_id LIMIT %s",
            (source, scope.environment, *((position,) if position else ()), size),
        ).fetchall()
    table = _LANES[lane]  # Fixed code identifiers only; never dynamic user input.
    if lane == len(_LANES) - 1:
        condition = (
            "(exchange,account_id,environment,product,producer)=(%s,%s,%s,%s,%s) "
            "AND signal_id IS NOT NULL"
        )
        ownership = tuple(asdict(scope).values())
    else:
        condition, ownership = "consumer=%s", (scope.consumer,)
    # LIMIT inside the parameterized PK lookup is an intentional planner
    # boundary. Put source filtering outside it: with stale source statistics,
    # PostgreSQL can otherwise rescan a whole source for every recovery UUID.
    return conn.execute(
        "WITH page AS MATERIALIZED (SELECT signal_id "
        f"FROM {table} WHERE {condition} {seek} ORDER BY signal_id LIMIT %s) "
        "SELECT p.signal_id::text,s.request_key FROM page p "
        "LEFT JOIN LATERAL (SELECT request_key,source,environment FROM v2_inbound_signals "
        "WHERE signal_id=p.signal_id LIMIT 1) s "
        "ON s.source=%s AND s.environment=%s ORDER BY p.signal_id",
        (
            *ownership,
            *((position,) if position else ()),
            size,
            source,
            scope.environment,
        ),
    ).fetchall()


def deployment_history_page(conn, *, scope, source, cursor, size):
    if type(size) is not int or not 1 <= size <= 10000:
        raise ValueError("STRATEGY_HISTORY_PAGE_SIZE_REQUIRED")
    cutoff = initial_signal_cutoff(conn, scope)
    state = _state(cursor, scope=scope, source=source, cutoff=cutoff)
    lane = state[1]
    physical = _lane_page(
        conn,
        scope=scope,
        source=source,
        cutoff=cutoff,
        lane=lane,
        position=state[3][lane],
        size=size,
    )
    # Round robin even when a lane remains full. A hot raw stream must not
    # starve original recovery work, nor large receipts starve raw discovery.
    state[1] = (lane + 1) % len(_LANES)
    rows, checkpoints = [], {}
    for row in physical:
        state[3][lane] = [row[2], row[0]] if lane == 0 else row[0]
        if row[1] is not None:
            rows.append((row[0], row[1]))
            checkpoints[row[0]] = _encode(state)
    if len(physical) < size:
        state[2] |= 1 << lane
        # Each lane wraps independently. A continuously full raw stream must
        # not leave an earlier empty recovery lane permanently marked done;
        # late commits/expired original work there still need bounded revisit.
        state[3][lane] = None
    return HistoryPage(
        rows,
        next_cursor=None if state[2] == _ALL_DONE else _encode(state),
        scanned=len(physical),
        checkpoints=checkpoints,
    )


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
