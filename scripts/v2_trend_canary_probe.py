"""Bounded SANDBOX v6 source probe; never authorizes or submits an order.

Run from the candidate release: python -m scripts.v2_trend_canary_probe
Only local PostgreSQL READ ONLY, Redis HGET, local ClickHouse SELECT, systemd
properties and capped journal reads. No exchange request or quota reservation.
Whole-universe 90s coverage is reported, NOT required for a source canary pass.
"""

import argparse
import json
import statistics
import subprocess
import time
from collections import Counter
from pathlib import Path

from services.v2_s0_regime import build_s0_state
from services.v2_s3_candles import STRUCTURE_VERSION
from v2_core.database import connection_factory
from v2_core.evidence import canonical, digest
from v2_core.ingress import symbol
from v2_core.producer import RedisMarketContext
from v2_core.trend_structure import validate_trend_structure

BENCHMARKS = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT")
VERSION_ERRORS = (
    "REFERENCE_FEATURE_VERSION_UNSUPPORTED",
    "REFERENCE_INPUT_MISMATCH",
    "TREND_STRUCTURE_UNCONFIRMED",
    "JEV_PUBLIC_INDICATOR",
    "mixed S3 feature versions",
    "MARKET_PROJECTION_UNCONFIRMED",
)


def journal(unit):
    result = subprocess.run(
        [
            "journalctl",
            "-u",
            unit,
            "--since",
            "2 minutes ago",
            "-n",
            "200",
            "--no-pager",
            "-o",
            "cat",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    lines = result.stdout.splitlines()
    rows = []
    for line in lines:
        try:
            row = json.loads(line)
            if isinstance(row, dict):
                rows.append(row)
        except ValueError:
            pass
    summary = {
        "lines": len(lines),
        "possibly_truncated": len(lines) >= 200,
        "statuses": dict(Counter(row.get("status", "UNSPECIFIED") for row in rows)),
        "version_errors": {
            code: sum(code in line for line in lines) for code in VERSION_ERRORS
        },
        "symbol_retry_reasons": dict(
            Counter(
                item.get("reason", "UNSPECIFIED")
                for row in rows
                for item in row.get("results", [])
                if item.get("status") == "RETRY"
            )
        ),
    }
    for key in ("elapsed_ms", "fresh", "pending"):
        values = sorted(row[key] for row in rows if type(row.get(key)) is int)
        summary[key] = (
            None
            if not values
            else {
                "min": values[0],
                "median": statistics.median(values),
                "p95": values[min(len(values) - 1, int(len(values) * 0.95))],
                "max": values[-1],
            }
        )
    return summary


def service(unit):
    result = subprocess.run(
        [
            "systemctl",
            "show",
            unit,
            "-p",
            "MainPID",
            "-p",
            "WorkingDirectory",
            "-p",
            "MemoryCurrent",
            "-p",
            "MemoryPeak",
            "-p",
            "CPUUsageNSec",
            "-p",
            "CPUQuotaPerSecUSec",
            "-p",
            "MemoryMax",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    values = dict(
        line.split("=", 1) for line in result.stdout.splitlines() if "=" in line
    )
    values["resolved_release"] = str(Path(values["WorkingDirectory"]).resolve())
    return values


def collect(targets, *, verify_btc=True):
    import redis

    now = time.time_ns() // 1000000
    connect = connection_factory(
        "dbname=trade_v2_testnet host=/var/run/postgresql port=55432",
        schema="trade_v2",
        read_only=True,
    )
    report = {
        "observed_at_ms": now,
        "environment": "SANDBOX",
        "execution_authorized": False,
        "errors": [],
    }
    with connect() as conn:
        conn.execute("SET LOCAL statement_timeout='2500ms'")
        assert (
            conn.execute("SELECT current_setting('transaction_read_only')").fetchone()[
                0
            ]
            == "on"
        )
        hub = conn.execute(
            "SELECT observed_at_ms,payload-'results'-'unsupported_symbols' FROM v2_market_hub_status WHERE environment='SANDBOX'"
        ).fetchone()
        report["hub"] = (
            {"observed_at_ms": hub[0], "age_ms": now - hub[0], **hub[1]}
            if hub
            else None
        )
        report["head_versions"] = [
            dict(
                zip(
                    ("frame_prefix", "status", "count", "fresh_90s", "oldest_age_ms"),
                    row,
                    strict=True,
                )
            )
            for row in conn.execute(
                "SELECT split_part(frame_id,':',1),status,count(*),count(*) FILTER(WHERE observed_at_ms BETWEEN %s AND %s),%s-min(observed_at_ms) FROM v2_market_symbol_heads WHERE environment='SANDBOX' GROUP BY 1,2",
                (now - 90000, now, now),
            ).fetchall()
        ]
        report["selected_heads"] = [
            dict(
                zip(
                    (
                        "symbol",
                        "observed_at_ms",
                        "status",
                        "reason",
                        "frame_id",
                        "age_ms",
                    ),
                    row,
                    strict=True,
                )
            )
            for row in conn.execute(
                "SELECT symbol,observed_at_ms,status,reason,frame_id,%s-observed_at_ms FROM v2_market_symbol_heads WHERE environment='SANDBOX' AND symbol=ANY(%s) ORDER BY symbol",
                (now, list(targets)),
            ).fetchall()
        ]
    cache = redis.Redis(
        unix_socket_path="/var/lib/trade-engine-v2/redis.sock",
        socket_connect_timeout=2,
        socket_timeout=2,
    )
    try:
        context = RedisMarketContext(cache, environment="SANDBOX")
        windows, native = {}, {}
        for target in sorted(set(targets) | set(BENCHMARKS)):
            envelope = context.read("s3", "SANDBOX", target)
            if envelope is None:
                native[target] = {"ready": False, "reason": "MISSING"}
                continue
            features = envelope["features"]
            evidence = features.get("input_evidence", {})
            age = now - envelope["observed_at"]
            row = {
                "version": evidence.get("window_version"),
                "observed_at_ms": envelope["observed_at"],
                "age_ms": age,
                "required_windows_present": all(
                    isinstance(features.get(name), dict)
                    for name in ("15m", "1h", "4h", "24h")
                ),
                "ready": False,
            }
            if target in BENCHMARKS:
                windows[target] = features
            if row["version"] == STRUCTURE_VERSION:
                try:
                    structure = validate_trend_structure(
                        features.get("trend_structure"),
                        symbol=target,
                        environment="SANDBOX",
                        closed_at_ms=envelope["observed_at"],
                    )
                    row.update(
                        structure_status=structure["status"],
                        last_closed_at_ms=structure["last_closed_at_ms"],
                    )
                    row["ready"] = (
                        structure["status"] == "READY"
                        and 0 <= age < 90000
                        and row["required_windows_present"]
                        and evidence.get("closed_at") == envelope["observed_at"]
                        and evidence.get("source") == "BINANCE_FUTURES"
                        and evidence.get("environment") == "SANDBOX"
                        and evidence.get("interval") == "1m"
                        and evidence.get("bar_count") == 1440
                    )
                except (ValueError, TypeError, KeyError) as exc:
                    row["reason"] = type(exc).__name__ + ":" + str(exc)
            native[target] = row
        report["native_context"] = native
        s0 = context.read("s0", "SANDBOX", "*")
        report["s0"] = {"ready": False}
        if s0 is not None:
            features = s0["features"]
            fingerprint = digest(canonical(windows))
            row = {
                "observed_at_ms": s0["observed_at"],
                "age_ms": now - s0["observed_at"],
                "regime": features.get("regime"),
                "feature_keys": sorted(features),
                "evidence": features.get("evidence"),
                "ready": False,
            }
            try:
                expected = build_s0_state(
                    windows,
                    universe=BENCHMARKS,
                    s3_frame_id="shared-basket:" + fingerprint,
                    s3_input_digest=fingerprint,
                )
                row["benchmark_same_v6_cutoff"] = all(
                    native[target]["version"] == STRUCTURE_VERSION
                    and native[target]["observed_at_ms"] == s0["observed_at"]
                    for target in BENCHMARKS
                )
                row["matches_current_benchmark_digest"] = (
                    features.get("evidence", {}).get("s3_input_digest") == fingerprint
                )
                row["ready"] = (
                    row["benchmark_same_v6_cutoff"]
                    and row["matches_current_benchmark_digest"]
                    and features.get("regime") == expected["regime"]
                    and 0 <= row["age_ms"] < 90000
                )
            except (ValueError, TypeError, KeyError) as exc:
                row["reason"] = type(exc).__name__ + ":" + str(exc)
            report["s0"] = row
    finally:
        cache.close()
    report["btc_reference"] = {"status": "NOT_RUN"}
    if verify_btc:
        import clickhouse_connect

        from services.v2_shared_market import IncrementalArchive
        from services.v2_shared_risk_reference import SharedRiskReference
        from v2_core.account_risk import AccountScope

        client = clickhouse_connect.get_client(
            host="127.0.0.1",
            port=18123,
            database="trade_v2_testnet",
            username="tradev2shadow",
            connect_timeout=3,
            send_receive_timeout=5,
            settings={
                "readonly": 1,
                "max_threads": 1,
                "max_execution_time": 3,
                "max_result_rows": 20000,
                "max_memory_usage": 134217728,
            },
        )
        try:
            scope = AccountScope("BINANCE", "read-only-preflight", "SANDBOX", "FUTURES")
            reader = SharedRiskReference(
                connect,
                scope=scope,
                archive=IncrementalArchive(client, "SANDBOX"),
                clock_ms=lambda: time.time_ns() // 1000000,
            )
            reference = reader(
                {
                    "exchange": "BINANCE",
                    "account_id": scope.account_id,
                    "environment": "SANDBOX",
                    "product": "FUTURES",
                    "leg": "OPEN",
                    "symbol": "BTCUSDT",
                }
            )
            report["btc_reference"] = {
                "status": "VERIFIED",
                "frame_id": reference.provenance.frame_id,
                "observed_at_ms": reference.observed_at_ms,
                "age_ms": time.time_ns() // 1000000 - reference.observed_at_ms,
            }
        except (ValueError, TypeError, KeyError) as exc:
            report["btc_reference"] = {
                "status": "RETRY",
                "reason": type(exc).__name__ + ":" + str(exc),
            }
        finally:
            client.close()
    report["services"] = {}
    for unit in (
        "trade-v2-market-hub.service",
        "trade-v2-execution-controller.service",
    ):
        report["services"][unit] = {
            "runtime": service(unit),
            "last_two_minutes": journal(unit),
        }
    reference_ok = report["btc_reference"].get("status") == "VERIFIED" and report[
        "btc_reference"
    ].get("frame_id", "").startswith("s3-shared-v6:")
    heads = {row["symbol"]: row for row in report["selected_heads"]}
    report["selected_pg_cache_converged"] = all(
        target in heads
        and heads[target]["status"] == "CURRENT"
        and heads[target]["frame_id"]
        == f"s3-shared-v6:{target}:{heads[target]['observed_at_ms']}"
        and heads[target]["observed_at_ms"]
        == report["native_context"].get(target, {}).get("observed_at_ms")
        and 0 <= heads[target]["age_ms"] < 90000
        for target in targets
    )
    report["source_canary_ready"] = (
        reference_ok
        and report["selected_pg_cache_converged"]
        and report["s0"]["ready"]
        and all(
            report["native_context"].get(target, {}).get("ready")
            for target in set(targets) | set(BENCHMARKS)
        )
        and not any(
            count
            for unit in report["services"].values()
            for count in unit["last_two_minutes"]["version_errors"].values()
        )
    )
    report["limitations"] = [
        "Non-atomic snapshots: repeat across a minute boundary before concluding convergence.",
        "Journal checks cover only emitted/capped logs, not every consumed strategy receipt.",
        "Source readiness is not market/account admission, execution permission, or proof of 100% universe freshness.",
    ]
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", default="BTCUSDT,ETHUSDT,REDUSDT")
    parser.add_argument("--skip-btc-reference", action="store_true")
    parser.add_argument("--require-ready", action="store_true")
    args = parser.parse_args()
    targets = tuple(dict.fromkeys(symbol(value) for value in args.symbols.split(",")))
    if not 1 <= len(targets) <= 12:
        parser.error("one to twelve symbols required")
    report = collect(targets, verify_btc=not args.skip_btc_reference)
    print(json.dumps(report, sort_keys=True, default=str))
    return 2 if args.require_ready and not report["source_canary_ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
