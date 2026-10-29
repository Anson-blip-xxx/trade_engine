"""Bounded non-overlapping prospective cohorts, PG-only checkpoints/evidence.

No trading calls, signal publication or exchange credentials. Each symbol
advances independently. Late history is retried; after 24h a missing sample is
explicitly recorded, never interpolated. 90-day bounded research-only retention.
"""

import json
import re
import time
from dataclasses import asdict

from v2_core.evidence import canonical, digest
from v2_core.exit_comparison import VERSION, ExitPolicy, compare_exits
from v2_core.ingress import symbol
from v2_core.quant_indicators import aggregate_minutes
from v2_core.quant_registry import parameters, warmup
from v2_core.quant_replay import ReplayPolicy
from v2_core.range_volume import RangeVolumePolicy, assess


def configuration(study_id, start_ms):
    return {
        "study_id": study_id,
        "version": VERSION,
        "start_ms": start_ms,
        "symbols": ["BTCUSDT", "ETHUSDT", "SOLUSDT"],
        "timeframe_minutes": 5,
        "window_minutes": 360,
        "archive_lag_minutes": 5,
        "gap_wait_minutes": 1440,
        "retention_days": 90,
        "policy": asdict(ReplayPolicy()),
        "exit_policy": asdict(ExitPolicy()),
        "range_policy": asdict(RangeVolumePolicy()),
        "parameters": parameters(),
        "execution_authorized": False,
    }


def validate(config):
    expected = configuration("placeholder", 0)
    if (
        set(config) != set(expected)
        or config["version"] != VERSION
        or config["execution_authorized"] is not False
    ):
        raise ValueError("SAMPLER_CONFIG_SCOPE")
    study_id = config["study_id"]
    if (
        not isinstance(study_id, str)
        or not 1 <= len(study_id) <= 80
        or not all(c.isalnum() or c in "-_" for c in study_id)
    ):
        raise ValueError("SAMPLER_ID")
    for key, low, high in (
        ("start_ms", 0, 2**53 - 1),
        ("window_minutes", 60, 720),
        ("archive_lag_minutes", 1, 60),
        ("gap_wait_minutes", 60, 2880),
        ("retention_days", 7, 180),
    ):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError("SAMPLER_BOUNDS:" + key)
    if (
        type(config["timeframe_minutes"]) is not int
        or config["timeframe_minutes"] != 5
        or config["start_ms"] % 300000
        or config["window_minutes"] % 5
    ):
        raise ValueError("SAMPLER_ALIGNMENT")
    targets = config["symbols"]
    if (
        not isinstance(targets, list)
        or not 1 <= len(targets) <= 3
        or len(set(targets)) != len(targets)
    ):
        raise ValueError("SAMPLER_SYMBOL_BOUND")
    for target in targets:
        symbol(target)
    policy = ReplayPolicy(**config["policy"])
    exits = ExitPolicy(**config["exit_policy"])
    p = parameters(config["parameters"])
    RangeVolumePolicy(**config["range_policy"])
    if policy.max_hold_bars > 100 or max(warmup(p), exits.volume_bars + 1) > 250:
        raise ValueError("SAMPLER_COMPUTE_BOUND")
    canonical(config)


def register(connect, config, *, first_activation_ms=None):
    validate(config)
    content = canonical(config)
    with connect() as conn:
        existing = conn.execute(
            "SELECT config_json FROM exit_studies WHERE study_id=%s",
            (config["study_id"],),
        ).fetchone()
        if (
            existing is None
            and first_activation_ms is not None
            and config["start_ms"] < first_activation_ms
        ):
            raise ValueError("SAMPLER_FORWARD_START_MUST_BE_FUTURE")
        conn.execute(
            "INSERT INTO exit_studies(study_id,config_json) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (config["study_id"], content),
        )
        if (
            conn.execute(
                "SELECT config_json FROM exit_studies WHERE study_id=%s",
                (config["study_id"],),
            ).fetchone()[0]
            != content
        ):
            raise ValueError("SAMPLER_FROZEN_CONFIG_CHANGED")
        for target in config["symbols"]:
            conn.execute(
                "INSERT INTO exit_cursors VALUES(%s,%s,%s) ON CONFLICT DO NOTHING",
                (config["study_id"], target, config["start_ms"]),
            )


def sample(archive, config, target, start_ms):
    validate(config)
    if archive.environment != "SANDBOX" or target not in config["symbols"]:
        raise ValueError("SAMPLER_ARCHIVE_SCOPE")
    if type(start_ms) is not int or start_ms % 300000:
        raise ValueError("SAMPLER_WINDOW_ALIGNMENT")
    policy = ReplayPolicy(**config["policy"])
    exits = ExitPolicy(**config["exit_policy"])
    p = config["parameters"]
    prior = max(
        warmup(p),
        exits.volume_bars + 1,
        RangeVolumePolicy(**config["range_policy"]).required,
    )
    end = start_ms + config["window_minutes"] * 60000
    read_start = start_ms - prior * 300000
    read_end = end + (policy.max_hold_bars + 1) * 300000
    required = (read_end - read_start) // 60000
    rows = archive.read(target, read_start, read_end)
    if len(rows) != required:
        return {
            "status": "HISTORY_GAP",
            "required_minutes": required,
            "available_minutes": len(rows),
            "execution_authorized": False,
        }
    if rows[-1]["t"] != read_start or rows[0]["t"] != read_end - 60000:
        raise ValueError("SAMPLER_ARCHIVE_BOUNDARY")
    bars = aggregate_minutes(
        list(reversed(rows)), timeframe_minutes=5, closed_at_ms=read_end
    )
    result = compare_exits(
        bars,
        timeframe_minutes=5,
        entry_start_ms=start_ms,
        entry_end_ms=end,
        policy=policy,
        exits=exits,
        params=p,
    )
    # Record P1 features without filtering the common entry cohort.
    range_policy = RangeVolumePolicy(**config["range_policy"])
    result["range_volume_observations"] = [
        assess(bars[max(0, i - 200) : i + 1], timeframe_minutes=5, policy=range_policy)
        for i, bar in enumerate(bars)
        if start_ms <= bar.t + 300000 < end
    ]
    return {
        "status": "SIMULATED",
        "symbol": target,
        "environment": "SANDBOX",
        "source_digest": digest(canonical({"minutes": rows})),
        "source_start_ms": read_start,
        "source_end_ms": read_end,
        **result,
    }


def save(connect, config, target, start_ms, mode, report):
    if (
        mode not in ("FORWARD", "CALIBRATION")
        or report.get("execution_authorized") is not False
        or report.get("status") not in ("SIMULATED", "HISTORY_GAP")
    ):
        raise ValueError("SAMPLER_PERSIST_SCOPE")
    if (
        target not in config["symbols"]
        or type(start_ms) is not int
        or start_ms % 300000
    ):
        raise ValueError("SAMPLER_PERSIST_TARGET")
    if mode == "FORWARD" and (
        start_ms < config["start_ms"]
        or (start_ms - config["start_ms"]) % (config["window_minutes"] * 60000)
    ):
        raise ValueError("SAMPLER_FORWARD_WINDOW")
    payload = canonical(
        {"config_digest": digest(canonical(config)), "mode": mode, "report": report}
    )
    if len(payload.encode()) > 2_000_000:
        raise ValueError("SAMPLER_RECORD_BOUND")
    key = (config["study_id"], target, start_ms, mode)
    with connect() as conn:
        conn.execute(
            "INSERT INTO exit_samples(study_id,symbol,start_ms,mode,status,payload_json,content_digest) VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (*key, report["status"], payload, digest(payload)),
        )
        stored = conn.execute(
            "SELECT content_digest FROM exit_samples WHERE study_id=%s AND symbol=%s AND start_ms=%s AND mode=%s",
            key,
        ).fetchone()[0]
        if stored != digest(payload):
            raise ValueError("SAMPLER_IMMUTABLE_CONFLICT")
        if mode == "FORWARD":
            next_ms = start_ms + config["window_minutes"] * 60000
            conn.execute(
                "UPDATE exit_cursors SET next_start_ms=%s WHERE study_id=%s AND symbol=%s AND next_start_ms=%s",
                (next_ms, config["study_id"], target, start_ms),
            )
    return stored


def run_once(
    connect, archive, config, *, now_ms, monotonic=time.monotonic, smoke_start_ms=None
):
    register(connect, config)
    started, outcomes = monotonic(), []
    for target in config["symbols"]:
        if monotonic() - started > 180:
            outcomes.append({"symbol": target, "status": "JOB_BUDGET_DEFERRED"})
            break
        try:
            with connect() as conn:
                start = conn.execute(
                    "SELECT next_start_ms FROM exit_cursors WHERE study_id=%s AND symbol=%s",
                    (config["study_id"], target),
                ).fetchone()[0]
            if smoke_start_ms is not None:
                start = smoke_start_ms
            ready = (
                start
                + (
                    config["window_minutes"]
                    + (config["policy"]["max_hold_bars"] + 1) * 5
                    + config["archive_lag_minutes"]
                )
                * 60000
            )
            if now_ms < ready:
                outcomes.append(
                    {
                        "symbol": target,
                        "status": "WAITING_FOR_CLOSED_HORIZON",
                        "ready_at_ms": ready,
                    }
                )
                continue
            report = sample(archive, config, target, start)
            if (
                report["status"] == "HISTORY_GAP"
                and now_ms < ready + config["gap_wait_minutes"] * 60000
            ):
                outcomes.append({"symbol": target, "status": "RETRY_HISTORY_GAP"})
                continue
            mode = "CALIBRATION" if smoke_start_ms is not None else "FORWARD"
            identity = save(connect, config, target, start, mode, report)
            outcomes.append(
                {
                    "symbol": target,
                    "status": report["status"],
                    "mode": mode,
                    "start_ms": start,
                    "content_digest": identity,
                    "summary": report.get("summary"),
                }
            )
        except Exception as exc:  # noqa: BLE001 - isolate symbols and sanitize driver errors
            # Do not log raw driver errors, DSNs or connection details.
            outcomes.append(
                {
                    "symbol": target,
                    "status": "RETRY_ERROR",
                    "error_type": type(exc).__name__,
                    "error_code": str(exc)
                    if re.fullmatch(r"[A-Z][A-Z0-9_:]{0,80}", str(exc))
                    else "DRIVER_OR_UNEXPECTED_ERROR",
                }
            )
    # Delete only this study's expired research evidence, bounded per invocation.
    with connect() as conn:
        conn.execute(
            "DELETE FROM exit_samples WHERE ctid IN (SELECT ctid FROM exit_samples WHERE study_id=%s AND created_at < now() - (%s * interval '1 day') ORDER BY created_at LIMIT 100)",
            (config["study_id"], config["retention_days"]),
        )
    return outcomes


def main():
    import argparse
    import os

    import clickhouse_connect

    from services.v2_shared_market import IncrementalArchive
    from v2_core.database import connection_factory

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--smoke-start-ms", type=int)
    args = parser.parse_args()
    with open(args.config, encoding="utf-8") as stream:
        config = json.load(stream)
    validate(config)
    connect = connection_factory(
        os.environ["V2_RESEARCH_PG_DSN"], schema="trade_v2_research"
    )
    with connect() as conn:
        if conn.execute(
            "SELECT current_database(),current_user,current_schema()"
        ).fetchone() != ("trade_v2_testnet", "tradev2research", "trade_v2_research"):
            raise ValueError("SAMPLER_DATABASE_SCOPE")
    register(connect, config, first_activation_ms=int(time.time() * 1000))
    client = clickhouse_connect.get_client(
        host="127.0.0.1",
        port=18123,
        username="tradev2research",
        database="trade_v2_testnet",
        connect_timeout=3,
        send_receive_timeout=10,
        settings={
            "readonly": 1,
            "max_threads": 1,
            "max_execution_time": 8,
            "max_memory_usage": 134217728,
        },
    )
    try:
        results = run_once(
            connect,
            IncrementalArchive(client, "SANDBOX"),
            config,
            now_ms=int(time.time() * 1000),
            smoke_start_ms=args.smoke_start_ms,
        )
        print(
            json.dumps(
                {"study_id": config["study_id"], "outcomes": results}, sort_keys=True
            ),
            flush=True,
        )
        if any(
            r["status"] in ("RETRY_ERROR", "RETRY_HISTORY_GAP", "HISTORY_GAP")
            for r in results
        ):
            raise SystemExit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
