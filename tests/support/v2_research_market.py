"""Synthetic frozen research inputs, independent of the deployed S3 producer.

Research consumes the recorded v6 structure contract; it does not run a market
publisher. Build the exact structural facts with the existing pure builder and
include only the frozen fields exercised by these research tests. This fixture
does not assert that the installed producer emits v6 or fabricate its unrelated
rolling indicators. Producer compatibility is tested by producer-specific tests.
"""

from v2_core.evidence import canonical, digest
from v2_core.trend_structure import build_trend_structure


def research_market_from_minutes(bars, *, symbol, environment, closed_at_ms):
    structure = build_trend_structure(
        bars, symbol=symbol, environment=environment, closed_at_ms=closed_at_ms
    )
    return {
        "15m": {"close": bars[-1]["c"]},
        "input_evidence": {
            "source": "BINANCE_FUTURES",
            "environment": environment,
            "window_version": "s3-closed-1m-v6",
            "interval": "1m",
            "closed_at": closed_at_ms,
            "bars_digest": digest(canonical({"bars": list(reversed(bars))})),
            "bar_count": len(bars),
        },
        "trend_structure": structure,
    }
