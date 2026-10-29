"""Read only real fills through sanitized views; write research tables only.

Every eligible real entry can be sampled, including currently open positions.
Facts are a bounded repeatable-read snapshot; no transaction spans archive I/O.
Pending failures retry independently and become explicit uncovered samples.
"""

import json
import re
import time
from dataclasses import asdict

from v2_core.evidence import canonical, digest
from v2_core.exit_comparison import ExitPolicy
from v2_core.filled_exit_shadow import VERSION, WIDTH, analyze, entry_from_facts
from v2_core.quant_indicators import aggregate_minutes
from v2_core.quant_registry import warmup
from v2_core.quant_replay import ReplayPolicy


def configuration(study_id, start_ms):
    return {
        "study_id": study_id,
        "version": VERSION,
        "start_ms": start_ms,
        "historical_hours": 48,
        "discovery_hours": 72,
        "batch": 3,
        "wait_hours": 24,
        "retention_days": 90,
        "policy": asdict(ReplayPolicy()),
        "exit_policy": asdict(ExitPolicy()),
        "execution_authorized": False,
    }


def validate(config):
    if (
        set(config) != set(configuration("id", 0))
        or config["version"] != VERSION
        or config["execution_authorized"] is not False
    ):
        raise ValueError("ACTUAL_CONFIG_SCOPE")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", config["study_id"]):
        raise ValueError("ACTUAL_STUDY_ID")
    for key, low, high in (
        ("start_ms", 0, 2**53 - 1),
        ("historical_hours", 0, 72),
        ("discovery_hours", 24, 168),
        ("batch", 1, 10),
        ("wait_hours", 1, 48),
        ("retention_days", 7, 180),
    ):
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError("ACTUAL_CONFIG_BOUND")
    if config["wait_hours"] >= config["discovery_hours"]:
        raise ValueError("ACTUAL_RETRY_DISCOVERY_BOUND")
    p = ReplayPolicy(**config["policy"])
    ExitPolicy(**config["exit_policy"])
    if p.max_hold_bars > 100:
        raise ValueError("ACTUAL_COMPUTE_BOUND")
    canonical(config)


def register(connect, config, *, first_activation_ms=None):
    validate(config)
    with connect() as conn:
        existing = conn.execute(
            "SELECT config_json FROM actual_exit_studies WHERE study_id=%s",
            (config["study_id"],),
        ).fetchone()
        if (
            existing is None
            and first_activation_ms is not None
            and config["start_ms"] < first_activation_ms
        ):
            raise ValueError("ACTUAL_REGISTRATION_START_MUST_BE_FUTURE")
        conn.execute(
            "INSERT INTO actual_exit_studies(study_id,config_json) VALUES(%s,%s) ON CONFLICT DO NOTHING",
            (config["study_id"], canonical(config)),
        )
        if conn.execute(
            "SELECT config_json FROM actual_exit_studies WHERE study_id=%s",
            (config["study_id"],),
        ).fetchone()[0] != canonical(config):
            raise ValueError("ACTUAL_FROZEN_CONFIG_CHANGED")


def discover(connect, config, now_ms):
    start = max(
        config["start_ms"] - config["historical_hours"] * 3600000,
        now_ms - config["discovery_hours"] * 3600000,
    )
    with connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        conn.execute("SET LOCAL statement_timeout='8s'")
        new = conn.execute(
            """SELECT i.intent_id::text FROM actual_exit_intents i
          LEFT JOIN actual_exit_jobs j ON j.intent_id=i.intent_id AND j.study_id=%s
          WHERE i.created_at<=to_timestamp(%s/1000.0)
          AND j.intent_id IS NULL AND i.created_at>=to_timestamp(%s/1000.0)
          ORDER BY i.created_at,i.intent_id LIMIT %s""",
            (config["study_id"], now_ms, start, config["batch"]),
        ).fetchall()
        pending = conn.execute(
            """SELECT intent_id::text FROM actual_exit_jobs
          WHERE study_id=%s AND status='PENDING' AND retry_after_ms<=%s
          ORDER BY retry_after_ms,intent_id LIMIT %s""",
            (config["study_id"], now_ms, config["batch"]),
        ).fetchall()
    # Interleaved lanes: missing archive for one entry cannot starve new entries.
    result = []
    lanes = (
        (pending, new)
        if config["batch"] == 1 and (now_ms // 1800000) % 2
        else (new, pending)
    )
    for i in range(config["batch"]):
        for rows in lanes:
            if i < len(rows):
                result.append(rows[i][0])
    return result[: config["batch"]]


def load_facts(connect, identity, now_ms):
    with connect() as conn:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        conn.execute("SET LOCAL statement_timeout='8s'")
        conn.execute("SET LOCAL lock_timeout='250ms'")
        row = conn.execute(
            """SELECT intent_id::text,scope_key,environment,producer,symbol,side,source,evidence_ref,
            config_digest,strategy_version,decided_ms,stop_price,maintenance,opening_terminal,
            (extract(epoch FROM created_at)*1000)::bigint,
            (extract(epoch FROM opening_observed_at)*1000)::bigint,tv_peer_present FROM actual_exit_intents
            WHERE intent_id=%s AND created_at<=to_timestamp(%s/1000.0)
            AND evidence_created_at<=to_timestamp(%s/1000.0)""",
            (identity, now_ms, now_ms),
        ).fetchone()
        if row is None:
            raise ValueError("ACTUAL_SOURCE_NOT_VISIBLE")
        keys = (
            "intent_id",
            "scope_key",
            "environment",
            "producer",
            "symbol",
            "side",
            "source",
            "evidence_ref",
            "config_digest",
            "strategy_version",
            "decided_ms",
            "stop_price",
            "maintenance",
            "opening_terminal",
            "created_ms",
            "opening_state_known_ms",
            "tv_peer_present",
        )
        facts = dict(zip(keys, row, strict=True))
        if (
            facts["opening_state_known_ms"] is None
            or facts["opening_state_known_ms"] > now_ms
        ):
            raise ValueError("ACTUAL_OPENING_STATE_NOT_YET_KNOWN")
        facts["decided_ms"] = int(facts["decided_ms"])
        rows = conn.execute(
            """SELECT identity,t,leg,quantity,price,fee,fee_currency FROM actual_exit_fills
            WHERE intent_id=%s AND t<=%s AND created_at<=to_timestamp(%s/1000.0)
            ORDER BY t,identity LIMIT 1001""",
            (identity, now_ms, now_ms),
        ).fetchall()
        facts["fills"] = [
            dict(
                zip(
                    (
                        "identity",
                        "t",
                        "leg",
                        "quantity",
                        "price",
                        "fee",
                        "fee_currency",
                    ),
                    f,
                    strict=True,
                )
            )
            for f in rows
        ]
        settlement = conn.execute(
            """SELECT revision,net_pnl FROM actual_exit_settlements
          WHERE intent_id=%s AND settled_at<=to_timestamp(%s/1000.0) ORDER BY revision DESC LIMIT 1""",
            (identity, now_ms),
        ).fetchone()
        facts["actual_settlement"] = (
            None
            if settlement is None
            else {"revision": settlement[0], "net_pnl": settlement[1]}
        )
        facts["observed_at_ms"] = now_ms
    return facts


def persist(connect, config, identity, report, *, now_ms, terminal=False):
    if report.get("execution_authorized") is not False:
        raise ValueError("ACTUAL_PERSIST_SCOPE")
    content = canonical(report)
    if len(content.encode()) > 2_000_000:
        raise ValueError("ACTUAL_RECORD_BOUND")
    with connect() as conn:
        old = conn.execute(
            "SELECT status,first_attempt_ms FROM actual_exit_jobs WHERE study_id=%s AND intent_id=%s FOR UPDATE",
            (config["study_id"], identity),
        ).fetchone()
        if old and old[0] in ("DONE", "UNCOVERED"):
            return old[0]
        first = old[1] if old else now_ms
        status = (
            "DONE"
            if report.get("status") == "ANALYZED"
            else "UNCOVERED"
            if terminal or now_ms - first >= config["wait_hours"] * 3600000
            else "PENDING"
        )
        conn.execute(
            """INSERT INTO actual_exit_jobs(study_id,intent_id,status,first_attempt_ms,retry_after_ms,payload_json,content_digest)
          VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(study_id,intent_id) DO UPDATE
          SET status=EXCLUDED.status,attempts=actual_exit_jobs.attempts+1,retry_after_ms=EXCLUDED.retry_after_ms,
            payload_json=EXCLUDED.payload_json,content_digest=EXCLUDED.content_digest
          WHERE actual_exit_jobs.status='PENDING'""",
            (
                config["study_id"],
                identity,
                status,
                first,
                now_ms + 1800000,
                content,
                digest(content),
            ),
        )
        return conn.execute(
            "SELECT status FROM actual_exit_jobs WHERE study_id=%s AND intent_id=%s",
            (config["study_id"], identity),
        ).fetchone()[0]


TERMINAL = {
    "SHADOW_FACT_SCOPE",
    "SHADOW_MAINTENANCE_EXCLUDED",
    "SHADOW_FILL_BOUND_OR_DUPLICATE",
    "SHADOW_FILL_INVALID",
    "SHADOW_INTERLEAVED_FILLS",
    "SHADOW_OPEN_FEE_CURRENCY_UNKNOWN",
    "SHADOW_STOP_NOT_ON_LOSS_SIDE",
    "SHADOW_TARGET_INVALID",
    "SHADOW_CLOSE_BEFORE_SAFE_WINDOW",
}


def run_once(connect, archive, config, *, now_ms, monotonic=time.monotonic):
    if archive.environment != "SANDBOX":
        raise ValueError("ACTUAL_ARCHIVE_SCOPE")
    register(connect, config)
    outcomes = []
    started = monotonic()
    for identity in discover(connect, config, now_ms):
        if monotonic() - started >= 210:
            break
        terminal = False
        try:
            facts = load_facts(connect, identity, now_ms)
            entry, _ = entry_from_facts(facts)
            safe = (entry["opened_at_ms"] + WIDTH - 1) // WIDTH * WIDTH
            end = safe + (config["policy"]["max_hold_bars"] + 1) * WIDTH
            start = safe - warmup() * WIDTH
            if now_ms < end + 300000:
                raise ValueError("ACTUAL_HORIZON_NOT_CLOSED")
            rows = archive.read(facts["symbol"], start, end)
            if len(rows) != (end - start) // 60000:
                raise ValueError("ACTUAL_ARCHIVE_INCOMPLETE")
            bars = aggregate_minutes(
                list(reversed(rows)), timeframe_minutes=5, closed_at_ms=end
            )
            report = analyze(
                facts,
                bars,
                policy=ReplayPolicy(**config["policy"]),
                exits=ExitPolicy(**config["exit_policy"]),
            )
            report.update(
                status="ANALYZED",
                study_id=config["study_id"],
                cohort="POST_REGISTRATION_REPLAY"
                if facts["created_ms"] >= config["start_ms"]
                else "HISTORICAL_DIAGNOSTIC",
            )
        except Exception as exc:  # noqa: BLE001 - per-episode retry, no driver details
            code = (
                str(exc)
                if re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", str(exc))
                else "ACTUAL_SOURCE_OR_COMPUTE_ERROR"
            )
            terminal = code in TERMINAL
            report = {
                "status": code,
                "error_type": type(exc).__name__,
                "execution_authorized": False,
            }
        status = persist(
            connect, config, identity, report, now_ms=now_ms, terminal=terminal
        )
        outcomes.append(
            {
                "status": status,
                "reason": report["status"],
                "source": report.get("facts", {}).get("source"),
                "baseline_funnel": {
                    k: v
                    for k, v in report.get("baseline_funnel", {}).items()
                    if k != "observations"
                },
            }
        )
    with connect() as conn:
        conn.execute(
            "DELETE FROM actual_exit_jobs WHERE ctid IN (SELECT ctid FROM actual_exit_jobs WHERE study_id=%s AND status!='PENDING' AND created_at<now()-(%s*interval '1 day') ORDER BY created_at LIMIT 100)",
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
    args = parser.parse_args()
    with open(args.config, encoding="utf-8") as stream:
        config = json.load(stream)
    validate(config)
    connect = connection_factory(
        os.environ["V2_ACTUAL_RESEARCH_DSN"], schema="trade_v2_research"
    )
    with connect() as conn:
        if conn.execute(
            "SELECT current_database(),current_user,current_schema()"
        ).fetchone() != ("trade_v2_testnet", "tradev2exitfacts", "trade_v2_research"):
            raise ValueError("ACTUAL_DATABASE_SCOPE")
    register(connect, config, first_activation_ms=time.time_ns() // 1000000)
    client = clickhouse_connect.get_client(
        host="127.0.0.1",
        port=18123,
        database="trade_v2_testnet",
        username="tradev2research",
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
        from services.v2_exit_funnel_audit import run_one

        audit_outcome = run_one(connect, IncrementalArchive(client, "SANDBOX"))
        results = run_once(
            connect,
            IncrementalArchive(client, "SANDBOX"),
            config,
            now_ms=time.time_ns() // 1000000,
        )
        print(
            json.dumps(
                {
                    "study_id": config["study_id"],
                    "outcomes": results,
                    "control_funnel_audit": audit_outcome,
                },
                sort_keys=True,
            ),
            flush=True,
        )
        if any(
            r["status"] == "PENDING" and r["reason"] != "ACTUAL_HORIZON_NOT_CLOSED"
            for r in results
        ):
            raise SystemExit(1)
    finally:
        client.close()


if __name__ == "__main__":
    main()
