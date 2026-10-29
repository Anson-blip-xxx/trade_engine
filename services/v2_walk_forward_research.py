"""Read-only, bounded archive walk-forward research; JSON output, no trade access.

The local ClickHouse database stores both explicit market source environments.
No account credentials, live market fetch, archive backfill or business writes
are used. Presets are engineering comparisons, not calibrated trading models.
"""

import argparse
import json
import re
from dataclasses import asdict
from datetime import datetime
from zoneinfo import ZoneInfo

from services.v2_shared_market import IncrementalArchive
from v2_core.evidence import canonical, digest
from v2_core.ingress import symbol
from v2_core.quant_indicators import VERSION as FEATURE_VERSION
from v2_core.quant_indicators import aggregate_minutes
from v2_core.quant_registry import PLUGINS, parameters, warmup
from v2_core.research_acceptance import (
    SCHEMA_VERSION,
    ResearchManifest,
    ResearchMetrics,
    assess,
)
from v2_core.walk_forward import MAX_BARS, walk_forward

VERSION = "walk-forward-archive-research-v1"
MINUTE = 60000
PAGE_MINUTES = 1000
TOTAL_MINUTE_BUDGET = 14400
PRESETS = ("donchian", "squeeze")
QUERY_SETTINGS = {
    "readonly": 1,
    "max_threads": 1,
    "max_execution_time": 5,
    "timeout_overflow_mode": "throw",
    "max_result_rows": 2000,
    "max_result_bytes": 2 * 1024 * 1024,
    "result_overflow_mode": "throw",
}
_UNPROVEN = {
    "historical_arrival_proven": False,
    "historical_universe_proven": False,
    "funding_complete": False,
    "cost_stress_tested": False,
    "independent_validation": False,
}


class _ReadOnlyClient:
    """Expose only bounded SELECT calls to IncrementalArchive."""

    def __init__(self, client, budget):
        self.client, self.budget = client, budget
        self.requested_minutes = self.queries = 0

    def query(self, sql, *, parameters):
        count = (parameters["end"] - parameters["start"]) // MINUTE
        if (
            not sql.startswith("SELECT ")
            or not 1 <= count <= PAGE_MINUTES
            or self.requested_minutes + count > self.budget
        ):
            raise ValueError("WALK_FORWARD_ARCHIVE_QUERY_BOUND")
        self.requested_minutes += count
        self.queries += 1
        return self.client.query(
            sql, parameters=parameters, settings=dict(QUERY_SETTINGS)
        )


def _scope(
    *,
    symbols,
    timeframe,
    end_ms,
    minutes,
    train_bars,
    test_bars,
    gap_bars,
    environment,
    candidates,
    policy,
    code_digest,
):
    if (
        not isinstance(symbols, (list, tuple))
        or not 1 <= len(symbols) <= 3
        or any(not isinstance(target, str) for target in symbols)
        or len(set(symbols)) != len(symbols)
        or environment not in ("LIVE", "SANDBOX")
        or type(timeframe) is not int
        or timeframe not in (1, 5, 15, 30, 60, 240, 1440)
        or type(minutes) is not int
        or not timeframe <= minutes <= TOTAL_MINUTE_BUDGET
        or minutes * len(symbols) > TOTAL_MINUTE_BUDGET
        or minutes // timeframe > MAX_BARS
        or type(end_ms) is not int
        or not 0 <= end_ms <= 2**53 - 1
        or end_ms % MINUTE
        or type(gap_bars) is not int
        or not 0 <= gap_bars <= MAX_BARS
        or any(
            value is not None and (type(value) is not int or not 1 <= value <= MAX_BARS)
            for value in (train_bars, test_bars)
        )
        or (
            code_digest is not None
            and (
                not isinstance(code_digest, str)
                or re.fullmatch(r"[0-9a-f]{64}", code_digest) is None
            )
        )
    ):
        raise ValueError("WALK_FORWARD_ARCHIVE_SCOPE_BOUND")
    for target in symbols:
        symbol(target)
    closed = end_ms // (timeframe * MINUTE) * timeframe * MINUTE
    if closed < minutes * MINUTE:
        raise ValueError("WALK_FORWARD_ARCHIVE_TIME_BOUND")
    try:
        utc8 = datetime.fromtimestamp(
            closed // 1000, ZoneInfo("Asia/Shanghai")
        ).isoformat()
    except (ValueError, OverflowError, OSError):
        raise ValueError("WALK_FORWARD_ARCHIVE_TIME_BOUND") from None
    if candidates is None:
        candidates = [
            {"id": name + "-default", "plugin": name, "params": {}} for name in PRESETS
        ]
    if (
        not isinstance(candidates, (list, tuple))
        or not 1 <= len(candidates) <= 2
        or any(
            not isinstance(candidate, dict)
            or candidate.get("plugin") not in PRESETS
            or candidate.get("plugin") not in PLUGINS
            for candidate in candidates
        )
    ):
        raise ValueError("WALK_FORWARD_ARCHIVE_CANDIDATES_BOUND")
    samples = minutes // timeframe
    required = max(
        warmup(parameters(candidate.get("params"))) for candidate in candidates
    )
    train = (
        min(samples, max(required + 1, samples // 2))
        if train_bars is None
        else train_bars
    )
    test = max(1, (samples - train - gap_bars) // 2) if test_bars is None else test_bars
    # Reuse the core's candidate/type validation before opening a DB connection.
    walk_forward(
        (),
        timeframe_minutes=timeframe,
        candidates=candidates,
        train_bars=train,
        test_bars=test,
        gap_bars=gap_bars,
        policy=policy,
    )
    return closed, utc8, candidates, train, test


def _fold_acceptance(result, *, target, environment, code_digest):
    reports = []
    for fold in result["folds"]:
        selected = fold["out_of_sample"]
        summary = selected["replay"]["summary"] if selected else None
        manifest = ResearchManifest(
            schema_version=SCHEMA_VERSION,
            run_id=f"archive-{target}-{fold['index']}-{fold['input_digest'][:16]}",
            code_digest=code_digest,
            data_digest=fold["input_digest"],
            feature_version=FEATURE_VERSION,
            source_environment=environment,
            metrics_basis="SIMULATED_OOS",
            capital_scope="SINGLE_SYMBOL",
            **_UNPROVEN,
            search_trials=len(fold["candidate_trials"]),
            train_end_ms=fold["train"]["end_ms_exclusive"],
            test_start_ms=fold["test"]["start_ms"],
            test_end_ms=fold["test"]["end_ms_exclusive"],
            sample_count=summary["closed_trades"] if summary else 0,
            fold_count=1,
            metrics=ResearchMetrics(
                realized_net_pnl=summary["realized_net_pnl"],
                max_drawdown_fraction=summary["max_drawdown_fraction"],
            )
            if summary
            else None,
        )
        reports.append(
            {
                "fold_index": fold["index"],
                "manifest": asdict(manifest),
                "assessment": assess(manifest),
            }
        )
    return reports


def run(
    client,
    *,
    symbols,
    environment,
    end_ms,
    timeframe=5,
    minutes=1440,
    train_bars=None,
    test_bars=None,
    gap_bars=0,
    candidates=None,
    policy=None,
    code_digest=None,
):
    closed, utc8, candidates, train, test = _scope(
        symbols=symbols,
        timeframe=timeframe,
        end_ms=end_ms,
        minutes=minutes,
        train_bars=train_bars,
        test_bars=test_bars,
        gap_bars=gap_bars,
        environment=environment,
        candidates=candidates,
        policy=policy,
        code_digest=code_digest,
    )
    start = closed - minutes * MINUTE
    reader = _ReadOnlyClient(client, minutes * len(symbols))
    archive = IncrementalArchive(reader, environment)
    reports = []
    for target in symbols:
        rows = []
        for page_start in range(start, closed, PAGE_MINUTES * MINUTE):
            page_end = min(closed, page_start + PAGE_MINUTES * MINUTE)
            page = archive.read(target, page_start, page_end)
            if any(
                type(row.get("t")) is not int
                or not page_start <= row["t"] < page_end
                or row["t"] % MINUTE
                for row in page
            ):
                raise ValueError("WALK_FORWARD_ARCHIVE_ROW_SCOPE")
            rows.extend(page)
        rows.sort(key=lambda row: row["t"])
        source = {
            "source_environment": environment,
            "symbol": target,
            "start_ms": start,
            "end_ms_exclusive": closed,
            "minutes": rows,
        }
        entry = {
            "symbol": target,
            "source_environment": environment,
            "input_digest": digest(canonical(source)),
            "required_minutes": minutes,
            "available_minutes": len(rows),
            "input_start_ms": start,
            "input_end_ms_exclusive": closed,
            **_UNPROVEN,
            "execution_authorized": False,
        }
        if len(rows) != minutes or any(
            row["t"] != start + index * MINUTE for index, row in enumerate(rows)
        ):
            reports.append({**entry, "status": "INCOMPLETE_HISTORY"})
            continue
        bars = aggregate_minutes(rows, timeframe_minutes=timeframe, closed_at_ms=closed)
        result = walk_forward(
            bars,
            timeframe_minutes=timeframe,
            candidates=candidates,
            train_bars=train,
            test_bars=test,
            gap_bars=gap_bars,
            policy=policy,
        )
        fold_acceptance = _fold_acceptance(
            result, target=target, environment=environment, code_digest=code_digest
        )
        reports.append(
            {
                **entry,
                "status": result["status"],
                "bar_count": len(bars),
                "raw_result_version": result["version"],
                "raw_result_digest": digest(canonical(result)),
                "walk_forward": result,
                "fold_count": result["fold_count"],
                "sample_count": sum(
                    row["manifest"]["sample_count"] for row in fold_acceptance
                ),
                "fold_acceptance": fold_acceptance,
            }
        )
    report = {
        "version": VERSION,
        "purpose": "READ_ONLY_ARCHIVE_OOS_RESEARCH_NOT_TRADING_ACCEPTANCE",
        "source_environment": environment,
        "code_digest": code_digest,
        "requested_end_ms": end_ms,
        "input_closed_at_ms": closed,
        "input_closed_at_utc8": utc8,
        "timeframe_minutes": timeframe,
        "train_bars": train,
        "test_bars": test,
        "gap_bars": gap_bars,
        "read_budget": {
            "requested_minutes": reader.requested_minutes,
            "maximum_minutes": TOTAL_MINUTE_BUDGET,
            "queries": reader.queries,
            "page_minutes": PAGE_MINUTES,
            "query_settings": dict(QUERY_SETTINGS),
        },
        **_UNPROVEN,
        "hmm_fitted": False,
        "execution_authorized": False,
        "portfolio_equity": None,
        "reports": reports,
        "counts": {
            "symbols": len(reports),
            "folds": sum(entry.get("fold_count", 0) for entry in reports),
            "selected_settled_oos_samples": sum(
                entry.get("sample_count", 0) for entry in reports
            ),
        },
        "limitations": [
            "Source environment is explicit; historical universe and data arrival are not proven.",
            "Funding, cost stress and independent validation are absent; acceptance remains incomplete.",
            "Configured fees/slippage and venue rules are simulation assumptions.",
            "Closed OOS trade counts exclude open positions and nonselected candidate baselines.",
            "Independent fold or symbol returns are not summed as portfolio equity.",
            "Code provenance is recorded only when explicitly supplied; no HMM was fitted.",
        ],
    }
    report["raw_report_digest"] = digest(canonical(report))
    return report


def _nonnegative(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected an integer") from None
    if number < 0:
        raise argparse.ArgumentTypeError("expected a nonnegative integer")
    return number


def _positive(value):
    number = _nonnegative(value)
    if number == 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", required=True, help="comma-separated, at most 3")
    parser.add_argument("--environment", required=True, choices=("LIVE", "SANDBOX"))
    parser.add_argument("--timeframe", type=_positive, default=5)
    parser.add_argument("--end-ms", type=_nonnegative, required=True)
    parser.add_argument("--minutes", type=_positive, default=1440)
    parser.add_argument("--train-bars", type=_positive)
    parser.add_argument("--test-bars", type=_positive)
    parser.add_argument("--gap-bars", type=_nonnegative, default=0)
    parser.add_argument("--code-digest")
    args = parser.parse_args(argv)
    kwargs = {
        "symbols": [target.strip() for target in args.symbols.split(",")],
        "environment": args.environment,
        "timeframe": args.timeframe,
        "end_ms": args.end_ms,
        "minutes": args.minutes,
        "train_bars": args.train_bars,
        "test_bars": args.test_bars,
        "gap_bars": args.gap_bars,
        "code_digest": args.code_digest,
    }
    try:
        _scope(**kwargs, candidates=None, policy=None)
    except (ValueError, TypeError) as exc:
        parser.error(str(exc))
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host="127.0.0.1",
        port=18123,
        database="trade_v2_testnet",
        connect_timeout=3,
        send_receive_timeout=5,
        query_retries=0,
        settings=dict(QUERY_SETTINGS),
    )
    try:
        print(json.dumps(run(client, **kwargs), sort_keys=True, allow_nan=False))
    finally:
        client.close()


if __name__ == "__main__":
    main()
