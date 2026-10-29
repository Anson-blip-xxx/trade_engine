"""Additive diagnosis of immutable P2 forward samples; never edits those samples."""

import json
import re

from v2_core.evidence import canonical, digest
from v2_core.exit_comparison import ExitPolicy
from v2_core.filled_exit_shadow import funnel
from v2_core.quant_indicators import aggregate_minutes

VERSION = "exit-trigger-funnel-v1"


def audit(archive, stored):
    if archive.environment != "SANDBOX" or stored.get("mode") != "FORWARD":
        raise ValueError("FUNNEL_AUDIT_SCOPE")
    report = stored["report"]
    if (
        report["version"] != "same-entry-exhaustion-v1"
        or report["execution_authorized"] is not False
    ):
        raise ValueError("FUNNEL_AUDIT_VERSION")
    start, end = report["source_start_ms"], report["source_end_ms"]
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 < end - start <= 3000 * 60000
    ):
        raise ValueError("FUNNEL_ARCHIVE_BOUND")
    rows = archive.read(report["symbol"], start, end)
    if (
        len(rows) != (end - start) // 60000
        or digest(canonical({"minutes": rows})) != report["source_digest"]
    ):
        raise ValueError("FUNNEL_ARCHIVE_MISSING_OR_CHANGED")
    bars = aggregate_minutes(
        list(reversed(rows)), timeframe_minutes=5, closed_at_ms=end
    )
    cases = []
    for pair in report["pairs"]:
        base = pair["baseline"]
        case = funnel(
            bars,
            base["entry"],
            exits=ExitPolicy(**report["exit_policy"]),
            end_ms=base["closed_bar_at_ms"] or end,
        )
        cases.append({"entry": base["entry"], "funnel": case})
    return {
        "version": VERSION,
        "status": "ANALYZED",
        "kind": "RESEARCH_BASELINE_NOT_ACTUAL_TRADE",
        "symbol": report["symbol"],
        "input_digest": report["input_digest"],
        "cases": cases,
        "execution_authorized": False,
    }


def run_one(connect, archive):
    with connect() as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        row = conn.execute(
            """SELECT s.content_digest,s.payload_json FROM actual_exit_control_samples s
         WHERE NOT EXISTS(SELECT 1 FROM exit_funnel_audits a WHERE a.source_digest=s.content_digest AND a.version=%s)
         ORDER BY s.start_ms,s.symbol LIMIT 1""",
            (VERSION,),
        ).fetchone()
    if row is None:
        return {"status": "NO_PENDING_CONTROL_SAMPLE"}
    try:
        report = audit(archive, json.loads(row[1]))
    except Exception as exc:  # noqa: BLE001 - keep original evidence intact, report unknown not zero
        code = (
            str(exc)
            if re.fullmatch(r"[A-Z][A-Z0-9_]{0,100}", str(exc))
            else "FUNNEL_ARCHIVE_OR_COMPUTE_ERROR"
        )
        report = {
            "version": VERSION,
            "status": "UNCOVERED",
            "reason": code,
            "execution_authorized": False,
        }
    content = canonical(report)
    if len(content.encode()) > 2_000_000:
        raise ValueError("FUNNEL_RECORD_BOUND")
    with connect() as conn:
        conn.execute(
            "INSERT INTO exit_funnel_audits(source_digest,version,status,payload_json,content_digest) VALUES(%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (row[0], VERSION, report["status"], content, digest(content)),
        )
        conn.execute(
            "DELETE FROM exit_funnel_audits WHERE ctid IN (SELECT ctid FROM exit_funnel_audits WHERE created_at<now()-interval '90 days' ORDER BY created_at LIMIT 100)"
        )
    return {
        "status": report["status"],
        "symbol": report.get("symbol"),
        "episodes": len(report.get("cases", [])),
    }
