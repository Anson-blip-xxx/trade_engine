"""Fair, bounded catalog observations independent of legacy S3/TV thresholds.

Each supported contract gets a deterministic 15-minute observation, not a BUY
or SELL recommendation. The causal builder separately reads complete closed
bars and decides whether a structure exists. No venue/network port is owned by
this scanner. An unfinished rotation survives window changes: a slow consumer
must not repeatedly revisit the alphabetic prefix and starve the tail. Completed
means one catalog rotation, NOT full coverage of a particular 15-minute window.
A cursor is disposable progress, never trading authority.
"""

from contextlib import nullcontext
from dataclasses import asdict
from hashlib import sha256

from services.v2_management_runtime import ManagementBinding
from v2_core.shared_market import Catalog
from v2_core.signals import Signals
from v2_core.strategy import StrategyScope
from v2_core.strategy_registry import strategy_identifier

_WINDOW = 900000
_CLOCK = "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"


def _page(symbols, before, window, limit):
    """Finite catalog seek; the timestamp partitions identities, not fairness."""
    if before is not None and before[0] > window:
        raise ValueError("STRATEGY_SCAN_CLOCK_REVERSED")
    same_window = before is not None and before[0] == window
    if same_window and before[2]:
        return [], before[1], True
    # Reset only after a completed rotation, never merely because time advanced.
    after = before[1] if before is not None and not before[2] else ""
    selected = [symbol for symbol in symbols if symbol > after][:limit]
    after = selected[-1] if selected else after
    completed = not any(symbol > after for symbol in symbols)
    return selected, after, completed


def scan_catalog(
    connect, *, binding, instance_id, guard, stop, limit=40, catalog_max_age_ms=120000
):
    if (
        not callable(connect)
        or type(binding) is not ManagementBinding
        or not callable(getattr(guard, "require_transaction", None))
        or getattr(guard, "_args", None) != binding.guard_args
        or not callable(getattr(stop, "is_set", None))
        or type(limit) is not int
        or not 1 <= limit <= 100
        or type(catalog_max_age_ms) is not int
        or not 1000 <= catalog_max_age_ms <= 7200000
    ):
        raise ValueError("STRATEGY_SCAN_CONFIG_REQUIRED")
    strategy_identifier(instance_id)
    scope = StrategyScope(**asdict(binding.scope), producer=instance_id)
    if stop.is_set():
        return {"status": "STOPPED", "signals": [], "execution_authorized": False}
    with connect() as conn:
        conn.execute("SET LOCAL lock_timeout='1s'")
        conn.execute("SET LOCAL statement_timeout='3s'")
        guard.require_transaction(conn, scope=binding.scope, operation="MANAGE")
        # Serialize this small DB-only batch. Never hold these locks across I/O.
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("strategy-catalog-scan:" + scope.consumer,),
        )
        now = conn.execute(_CLOCK).fetchone()[0]
        window = now // _WINDOW * _WINDOW
        connection = lambda: nullcontext(conn)
        catalog = Catalog(connection, "SANDBOX", lambda: now).snapshot(
            catalog_max_age_ms
        )
        symbols = sorted(
            symbol
            for symbol, row in catalog["contracts"].items()
            if row.get("supported") is True
        )
        before = conn.execute(
            "SELECT window_ms,after_symbol,completed FROM v2_strategy_scan_cursors "
            "WHERE consumer=%s FOR UPDATE",
            (scope.consumer,),
        ).fetchone()
        selected, after, completed = _page(symbols, before, window, limit)
        if before is not None and before[0] == window and before[2]:
            return {
                "status": "COMPLETED",
                "signals": [],
                "window_ms": window,
                "eligible_contracts": len(symbols),
                "completion_scope": "CATALOG_ROTATION_NOT_WINDOW_COVERAGE",
                "execution_authorized": False,
            }
        inbox, emitted = Signals(connection), []
        for symbol in selected:
            if stop.is_set():
                raise ValueError("STRATEGY_SCAN_STOPPED")
            key = f"structure-v1:{window}:" + sha256(symbol.encode()).hexdigest()
            emitted.append(
                inbox.admit(
                    source="quant",
                    environment="SANDBOX",
                    request_key=key,
                    snapshot={
                        "observed_at": window,
                        "expires_at_ms": window + _WINDOW,
                        "symbol": symbol,
                        "signal": "STRUCTURE_SCAN",
                        "features": {
                            "schema": "strategy-catalog-observation-v1",
                            "closed_at_ms": window,
                        },
                    },
                )
            )
        if stop.is_set():
            raise ValueError("STRATEGY_SCAN_STOPPED")
        if not window <= conn.execute(_CLOCK).fetchone()[0] < window + _WINDOW:
            raise ValueError("STRATEGY_SCAN_WINDOW_EXPIRED")
        conn.execute(
            "INSERT INTO v2_strategy_scan_cursors(consumer,window_ms,after_symbol,completed) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT (consumer) DO UPDATE SET "
            "window_ms=EXCLUDED.window_ms,after_symbol=EXCLUDED.after_symbol,"
            "completed=EXCLUDED.completed,updated_at=clock_timestamp()",
            (scope.consumer, window, after, completed),
        )
        return {
            "status": "COMPLETED" if completed else "MORE",
            "signals": emitted,
            "window_ms": window,
            "eligible_contracts": len(symbols),
            "completion_scope": "CATALOG_ROTATION_NOT_WINDOW_COVERAGE",
            "execution_authorized": False,
        }
