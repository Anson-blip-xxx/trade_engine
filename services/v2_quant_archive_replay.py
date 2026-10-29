"""Bounded READ-ONLY Sandbox archive smoke replay; never a profitability permit.

Fees, slippage, lot/tick and funding defaults are simulation assumptions. This
command intentionally labels results incomplete for historical profitability:
it does not reconstruct listing eligibility or obtain historical funding rates.
"""

import argparse
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from services.v2_shared_market import IncrementalArchive
from v2_core.evidence import canonical, digest
from v2_core.ingress import symbol
from v2_core.quant_indicators import aggregate_minutes
from v2_core.quant_registry import PLUGINS
from v2_core.quant_replay import ReplayPolicy, replay


def run(
    client, *, symbols, timeframe, end_ms, minutes=2880, plugins=PLUGINS, policy=None
):
    if (
        not isinstance(symbols, (list, tuple))
        or not 1 <= len(symbols) <= 10
        or len(set(symbols)) != len(symbols)
        or type(minutes) is not int
        or not 1440 <= minutes <= 14400
        or type(timeframe) is not int
        or timeframe not in (1, 5, 15, 30, 60)
        or type(end_ms) is not int
        or end_ms < minutes * 60000
        or end_ms % 60000
        or not plugins
        or not set(plugins) <= set(PLUGINS)
    ):
        raise ValueError("QUANT_REPLAY_SCOPE_BOUND")
    reports = []
    archive = IncrementalArchive(client, "SANDBOX")
    closed = end_ms // (timeframe * 60000) * timeframe * 60000
    for target in symbols:
        symbol(target)
        rows = archive.read(target, closed - minutes * 60000, closed)
        if len(rows) != minutes:
            reports.append(
                {
                    "symbol": target,
                    "status": "INCOMPLETE_HISTORY",
                    "required_minutes": minutes,
                    "available_minutes": len(rows),
                }
            )
            continue
        bars = aggregate_minutes(
            list(reversed(rows)), timeframe_minutes=timeframe, closed_at_ms=closed
        )
        source_digest = digest(canonical({"minutes": rows}))
        for plugin in plugins:
            result = replay(
                bars,
                timeframe_minutes=timeframe,
                plugin=plugin,
                policy=policy or ReplayPolicy(),
            )
            reports.append(
                {
                    "symbol": target,
                    "status": "SIMULATED",
                    "plugin": plugin,
                    "timeframe_minutes": timeframe,
                    "input_digest": source_digest,
                    "bar_count": len(bars),
                    "summary": result["summary"],
                    "open_position": result["open_position"] is not None,
                    "pending_signal": result["pending_signal"],
                    "policy": result["policy"],
                    "parameters": result["parameters"],
                    "assumptions": result["assumptions"],
                }
            )
    return {
        "purpose": "SANDBOX_ARCHIVE_ENGINEERING_SMOKE_NOT_PROFITABILITY",
        "execution_authorized": False,
        "input_closed_at_utc8": datetime.fromtimestamp(
            closed / 1000, ZoneInfo("Asia/Shanghai")
        ).isoformat(),
        "reports": reports,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True, help="comma-separated, at most 10")
    parser.add_argument("--timeframe", type=int, default=5)
    parser.add_argument("--end-ms", type=int, required=True)
    parser.add_argument("--minutes", type=int, default=1440)
    args = parser.parse_args()
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host="127.0.0.1",
        port=18123,
        database="trade_v2_testnet",
        connect_timeout=3,
        send_receive_timeout=8,
        settings={"readonly": 1, "max_threads": 1, "max_execution_time": 5},
    )
    try:
        print(
            json.dumps(
                run(
                    client,
                    symbols=args.symbols.split(","),
                    timeframe=args.timeframe,
                    end_ms=args.end_ms,
                    minutes=args.minutes,
                ),
                sort_keys=True,
            )
        )
    finally:
        client.close()


if __name__ == "__main__":
    main()
