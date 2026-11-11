"""Bounded read-only review progress; shadow differences are NOT actual profit.

No orders, model calls, parameter writes, scheduler, or notification authority.
The research jobs table is mutable: this reports the CURRENT database snapshot,
not a reconstruction of what was known at a historical timestamp.
"""

import argparse
import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal, localcontext
from zoneinfo import ZoneInfo

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical
from v2_core.symbols import is_supported_symbol

VERSION = "review-progress-v1"
VARIANTS = ("partial", "tighten")
MAX_RECORDS = 500
_HEX = re.compile(r"[0-9a-f]{32,64}")

# Hash execution, not explanatory labels such as reason/gross_pnl. Every fill
# must be complete; bool_and(... IS TRUE) must not silently ignore missing keys.
_EXECUTION_PATH_SQL = """SELECT
 jsonb_agg(jsonb_build_object('bar_at_ms',f.value->'bar_at_ms',
   'quantity',f.value->'quantity','price',f.value->'price','fee',f.value->'fee')
   ORDER BY f.ordinality) fills,
 (count(*)>0 AND coalesce(bool_and((
   jsonb_typeof(f.value)='object'
   AND jsonb_typeof(f.value->'bar_at_ms')='number'
   AND f.value->>'bar_at_ms' ~ '^[0-9]{1,16}$'
   AND jsonb_typeof(f.value->'quantity')='string'
   AND length(f.value->>'quantity')<=80
   AND f.value->>'quantity' ~ '^(0|[1-9][0-9]*)([.][0-9]+)?$'
   AND f.value->>'quantity' ~ '[1-9]'
   AND jsonb_typeof(f.value->'price')='string'
   AND length(f.value->>'price')<=80
   AND f.value->>'price' ~ '^(0|[1-9][0-9]*)([.][0-9]+)?$'
   AND f.value->>'price' ~ '[1-9]'
   AND jsonb_typeof(f.value->'fee')='string'
   AND length(f.value->>'fee')<=80
   AND f.value->>'fee' ~ '^-?(0|[1-9][0-9]*)([.][0-9]+)?$'
 ) IS TRUE),false)
 AND jsonb_typeof(v.value->'remaining_quantity')='string'
 AND v.value->>'remaining_quantity' ~ '^0([.]0+)?$'
 AND jsonb_typeof(v.value->'closed_bar_at_ms')='number'
 AND v.value->>'closed_bar_at_ms' ~ '^[0-9]{1,16}$') valid
 FROM jsonb_array_elements(CASE WHEN jsonb_typeof(v.value->'fills')='array'
   THEN v.value->'fills' ELSE '[]'::jsonb END) WITH ORDINALITY f(value,ordinality)"""


def _digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _number(value):
    if not isinstance(value, str) or len(value) > 100:
        raise ValueError("REVIEW_PROGRESS_NUMBER_UNKNOWN")
    result = Decimal(value)
    if not result.is_finite() or abs(result) >= Decimal("1e30"):
        raise ValueError("REVIEW_PROGRESS_NUMBER_INVALID")
    return result


def _scope(scope):
    if not isinstance(scope, AccountScope) or (
        scope.exchange,
        scope.environment,
        scope.product,
    ) != ("BINANCE", "SANDBOX", "FUTURES"):
        raise ValueError("REVIEW_PROGRESS_SANDBOX_SCOPE_REQUIRED")


def _identity(record):
    """Separate strategy, research definition, source, and registration cohorts."""
    return {
        key: record.get(key)
        for key in (
            "study_id",
            "cohort",
            "producer",
            "source",
            "strategy_version",
            "config_digest",
            "research_version",
            "study_digest",
            "policy_digest",
            "exit_policy_digest",
            "parameters_digest",
            "cost_digest",
            "symbol",
        )
    }


def _definition(record):
    """Missing definitions never silently share a supposedly compatible cohort."""
    required = _identity(record)
    if any(not isinstance(value, str) or not value for value in required.values()):
        return False
    return all(
        _HEX.fullmatch(required[key]) is not None
        for key in required
        if key.endswith("digest")
    )


def _pair(record, name):
    variants = record.get("variants")
    if not isinstance(variants, dict):
        raise TypeError("VARIANT_MISSING")
    base, alternate = variants.get("baseline"), variants.get(name)
    if not isinstance(base, dict) or not isinstance(alternate, dict):
        raise TypeError("VARIANT_MISSING")
    if base.get("status") != "CLOSED" or alternate.get("status") != "CLOSED":
        raise ValueError("PAIR_NOT_CLOSED")
    expected = _number(alternate.get("net_realized")) - _number(
        base.get("net_realized")
    )
    if _number(alternate.get("paired_net_delta")) != expected:
        raise ValueError("PAIR_DELTA_INCONSISTENT")
    before, after = base.get("path_digest"), alternate.get("path_digest")
    if any(
        not isinstance(v, str) or _HEX.fullmatch(v) is None for v in (before, after)
    ):
        raise ValueError("PAIR_PATH_UNKNOWN")
    changed = before != after
    if not changed and expected != 0:
        raise ValueError("IDENTICAL_PATH_DIFFERENT_NET")
    return expected, changed


def build_progress(
    records,
    *,
    scope,
    scope_key,
    start_ms,
    as_of_ms,
    minimum_effective_pairs=30,
    truncated=False,
):
    """Reduce one explicitly scoped current snapshot. Never output identities.

    'Effective' means execution path changed, not necessarily a nonzero PnL
    difference. A zero-delta changed path must not be counted as a no-op.
    The minimum is a reporting floor, not statistical power or promotion proof.
    """
    _scope(scope)
    if (
        not isinstance(records, (list, tuple))
        or len(records) > MAX_RECORDS
        or not isinstance(scope_key, str)
        or _HEX.fullmatch(scope_key) is None
        or type(start_ms) is not int
        or type(as_of_ms) is not int
        or not 0 <= start_ms < as_of_ms <= 2**53 - 1
        or type(minimum_effective_pairs) is not int
        or not 1 <= minimum_effective_pairs <= MAX_RECORDS
        or type(truncated) is not bool
    ):
        raise ValueError("REVIEW_PROGRESS_BOUND_INVALID")
    groups, seen, excluded = defaultdict(list), {}, Counter()
    for record in records:
        if not isinstance(record, dict):
            raise TypeError("REVIEW_PROGRESS_RECORD_INVALID")
        if (
            record.get("scope_key") != scope_key
            or record.get("environment") != scope.environment
        ):
            raise ValueError("REVIEW_PROGRESS_SCOPE_MISMATCH")
        if (
            not isinstance(record.get("symbol"), str)
            or not is_supported_symbol(record["symbol"])
        ):
            raise ValueError("REVIEW_PROGRESS_SYMBOL_INVALID")
        identity = (record.get("study_id"), record.get("intent_id"))
        if any(not isinstance(v, str) or not v for v in identity):
            raise ValueError("REVIEW_PROGRESS_IDENTITY_MISSING")
        encoded = canonical(record)
        if identity in seen:
            if seen[identity] != encoded:
                raise ValueError("REVIEW_PROGRESS_DUPLICATE_CONFLICT")
            excluded["duplicate_identical"] += 1
            continue
        seen[identity] = encoded
        created = record.get("created_ms")
        if type(created) is not int or not start_ms <= created <= as_of_ms:
            excluded["outside_intent_window"] += 1
            continue
        if record.get("maintenance") is not False:
            excluded["maintenance_or_unknown"] += 1
            continue
        groups[canonical(_identity(record))].append(record)
    output = []
    included = [row for group in groups.values() for row in group]
    with localcontext() as context:
        context.prec = 80
        for key, rows in sorted(groups.items()):
            statuses = Counter(row.get("status", "UNKNOWN") for row in rows)
            for name in VARIANTS:
                gaps, deltas, changes = Counter(), [], []
                for row in rows:
                    if row.get("status") != "DONE":
                        continue
                    if not _definition(row):
                        gaps["RESEARCH_DEFINITION_UNKNOWN"] += 1
                        continue
                    try:
                        delta, changed = _pair(row, name)
                    except (ValueError, TypeError, ArithmeticError) as error:
                        code = str(error)
                        gaps[
                            code
                            if code
                            in {
                                "VARIANT_MISSING",
                                "PAIR_NOT_CLOSED",
                                "PAIR_DELTA_INCONSISTENT",
                                "PAIR_PATH_UNKNOWN",
                                "IDENTICAL_PATH_DIFFERENT_NET",
                                "REVIEW_PROGRESS_NUMBER_UNKNOWN",
                                "REVIEW_PROGRESS_NUMBER_INVALID",
                            }
                            else "PAIR_DATA_INVALID"
                        ] += 1
                        continue
                    deltas.append(delta)
                    changes.append(changed)
                total = sum(deltas, Decimal(0)) if deltas else None
                effective = sum(changes)
                conclusion = (
                    "DO_NOT_PROMOTE_NEGATIVE"
                    if total is not None and total < 0
                    else "INSUFFICIENT_ACTIONABLE_EVIDENCE"
                    if effective < minimum_effective_pairs
                    or gaps
                    or truncated
                    or statuses.get("UNCOVERED", 0)
                    or statuses.get("UNKNOWN", 0)
                    else "OBSERVE_ONLY"
                )
                triggers = {}
                for field in ("research_trigger_count", "actual_holding_trigger_count"):
                    values = [
                        row.get(field) for row in rows if row.get("status") == "DONE"
                    ]
                    known = [v for v in values if type(v) is int and v >= 0]
                    triggers[field] = {
                        "known_samples": len(known),
                        "unknown_samples": len(values) - len(known),
                        "triggered_samples": sum(v > 0 for v in known),
                    }
                comparable_triggers = [
                    row
                    for row in rows
                    if row.get("status") == "DONE"
                    and all(
                        type(row.get(field)) is int and row[field] >= 0
                        for field in (
                            "research_trigger_count",
                            "actual_holding_trigger_count",
                        )
                    )
                ]
                output.append(
                    {
                        "group_id": _digest(
                            {"cohort": json.loads(key), "variant": name}
                        ),
                        "definition": json.loads(key),
                        "variant": name,
                        "job_counts": dict(sorted(statuses.items())),
                        "waiting_horizon": sum(
                            row.get("status") == "PENDING"
                            and row.get("reason") == "ACTUAL_HORIZON_NOT_CLOSED"
                            for row in rows
                        ),
                        "pending_other": sum(
                            row.get("status") == "PENDING"
                            and row.get("reason") != "ACTUAL_HORIZON_NOT_CLOSED"
                            for row in rows
                        ),
                        "paired_samples": len(deltas),
                        "effective_pairs": effective,
                        "no_op_pairs": len(deltas) - effective,
                        "positive_pairs": sum(v > 0 for v in deltas),
                        "negative_pairs": sum(v < 0 for v in deltas),
                        "zero_delta_pairs": sum(v == 0 for v in deltas),
                        "paired_net_delta_quote_units": str(total)
                        if total is not None
                        else None,
                        "quote_currency": "UNKNOWN",
                        "coverage_gaps": dict(sorted(gaps.items())),
                        "trigger_coverage": triggers,
                        "trigger_mismatch": {
                            "both_known_samples": len(comparable_triggers),
                            "research_trigger_without_actual_holding_trigger_samples": sum(
                                row["research_trigger_count"] > 0
                                and row["actual_holding_trigger_count"] == 0
                                for row in comparable_triggers
                            ),
                            "interpretation": "Investigate whether the research exit becomes actionable only after the real full-inventory holding interval; counts alone do not prove exact timing.",
                        },
                        "conclusion": conclusion,
                        "next_action": {
                            "DO_NOT_PROMOTE_NEGATIVE": "Retain baseline; inspect loss-making paired cases before proposing another frozen variant.",
                            "INSUFFICIENT_ACTIONABLE_EVIDENCE": "Collect changed-path pairs and resolve unknown coverage; do not tune against these same outcomes.",
                            "OBSERVE_ONLY": "Validate on independent future cohorts with complete costs and portfolio constraints; no automatic promotion.",
                        }[conclusion],
                    }
                )
    return {
        "schema": VERSION,
        "kind": "CURRENT_SHADOW_REVIEW_PROGRESS_NOT_ACTUAL_PNL",
        "as_of_ms": as_of_ms,
        "window_start_ms": start_ms,
        "as_of": datetime.fromtimestamp(
            as_of_ms / 1000, ZoneInfo("Asia/Shanghai")
        ).isoformat(),
        "timezone": "Asia/Shanghai",
        "environment": "SANDBOX",
        "account_scoped": True,
        "minimum_effective_pairs": minimum_effective_pairs,
        "truncated": truncated,
        "job_counts": dict(
            sorted(Counter(row.get("status", "UNKNOWN") for row in included).items())
        ),
        "waiting_horizon": sum(
            row.get("status") == "PENDING"
            and row.get("reason") == "ACTUAL_HORIZON_NOT_CLOSED"
            for row in included
        ),
        "excluded_records": dict(sorted(excluded.items())),
        "groups": output,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
        "limitations": [
            "Current mutable research job snapshot, not historical knowledge reconstruction.",
            "Shadow net differences are not actual additional profit or causal improvement.",
            "No-op samples do not establish that an alternative exit would have helped.",
            "Effective means a changed simulated path; counts do not establish independence or statistical significance.",
            "Costs are research assumptions; funding, historical venue filters and portfolio reuse remain unproven.",
            "Pending unclosed horizons are expected censoring, not trading failures.",
            "Registration cohorts, strategy versions, configurations and research/cost definitions are never pooled.",
            "Frozen quote-asset evidence is unavailable: deltas are per-instrument quote units, never labeled USDT or pooled across instruments.",
            "Actual-holding trigger coverage ends at first partial close or the fixed research horizon.",
        ],
    }


def load_progress(conn, scope, *, days=14, limit=500, minimum_effective_pairs=30):
    """Read sanitized views only, at one bounded repeatable-read snapshot.

    Caller must establish READ ONLY before calling. No raw evidence arrays or
    account identifiers are returned; account input is parameterized, never SQL.
    """
    _scope(scope)
    if (
        type(days) is not int
        or not 1 <= days <= 90
        or type(limit) is not int
        or not 1 <= limit <= MAX_RECORDS
    ):
        raise ValueError("REVIEW_PROGRESS_QUERY_BOUND")
    if conn.execute("SHOW transaction_read_only").fetchone()[0] != "on":
        raise ValueError("REVIEW_PROGRESS_READ_ONLY_REQUIRED")
    if conn.execute("SHOW transaction_isolation").fetchone()[0] not in {
        "repeatable read",
        "serializable",
    }:
        raise ValueError("REVIEW_PROGRESS_STABLE_SNAPSHOT_REQUIRED")
    as_of, scope_key = conn.execute(
        "SELECT (extract(epoch FROM transaction_timestamp())*1000)::bigint, "
        "md5(jsonb_build_array(%s::text,%s::text,%s::text,%s::text)::text)",
        tuple(asdict(scope).values()),
    ).fetchone()
    start = max(0, as_of - days * 86400000)
    rows = conn.execute(
        """SELECT j.study_id,i.intent_id::text,i.scope_key,i.environment,i.producer,i.source,
          i.strategy_version,i.config_digest,i.symbol,(extract(epoch FROM i.created_at)*1000)::bigint,
          i.maintenance,j.status,st.config_json::jsonb,p.doc->>'cohort',p.doc->>'version',
          p.doc->'policy',p.doc->'exit_policy',p.doc->'parameters',p.doc->>'status',
          p.doc#>'{baseline_funnel,trigger_count}',p.doc#>'{actual_full_inventory_funnel,trigger_count}',
          (SELECT jsonb_object_agg(v.key,jsonb_build_object(
            'status',v.value->>'status','net_realized',v.value->>'net_realized',
            'paired_net_delta',v.value->>'paired_net_delta','path_digest',
            CASE WHEN execution_path.valid THEN
              md5(jsonb_build_object('fills',execution_path.fills,
                'remaining_quantity',v.value->'remaining_quantity',
                'closed_bar_at_ms',v.value->'closed_bar_at_ms')::text) END))
            FROM jsonb_each(CASE WHEN jsonb_typeof(p.doc->'variants')='object'
              THEN p.doc->'variants' ELSE '{}'::jsonb END) v
            CROSS JOIN LATERAL (__EXECUTION_PATH_SQL__) execution_path
            WHERE v.key IN ('baseline','partial','tighten'))
        FROM trade_v2_research.actual_exit_jobs j
        JOIN trade_v2_research.actual_exit_intents i USING(intent_id)
        JOIN trade_v2_research.actual_exit_studies st USING(study_id)
        CROSS JOIN LATERAL (SELECT j.payload_json::jsonb doc) p
        WHERE i.scope_key=%s AND i.environment='SANDBOX' AND NOT i.maintenance
          AND i.created_at>=to_timestamp(%s/1000.0) AND i.created_at<=to_timestamp(%s/1000.0)
          AND i.evidence_created_at<=to_timestamp(%s/1000.0)
          AND j.created_at<=to_timestamp(%s/1000.0)
        ORDER BY i.created_at DESC,j.study_id,i.intent_id LIMIT %s""".replace(
            "__EXECUTION_PATH_SQL__", _EXECUTION_PATH_SQL
        ),
        (scope_key, start, as_of, as_of, as_of, limit + 1),
    ).fetchall()
    records = []
    names = (
        "study_id",
        "intent_id",
        "scope_key",
        "environment",
        "producer",
        "source",
        "strategy_version",
        "config_digest",
        "symbol",
        "created_ms",
        "maintenance",
        "status",
        "study",
        "cohort",
        "research_version",
        "policy",
        "exit_policy",
        "parameters",
        "reason",
        "research_trigger_count",
        "actual_holding_trigger_count",
        "variants",
    )
    for row in rows[:limit]:
        record = dict(zip(names, row, strict=True))
        study = record.pop("study")
        record["variants"] = record.get("variants") or {}
        if (
            not record["cohort"]
            and isinstance(study, dict)
            and type(study.get("start_ms")) is int
        ):
            record["cohort"] = (
                "POST_REGISTRATION_REPLAY"
                if record["created_ms"] >= study["start_ms"]
                else "PRE_REGISTRATION_REPLAY"
            )
        if not record["research_version"] and isinstance(study, dict):
            record["research_version"] = study.get("version")
        record["study_digest"] = _digest(study) if isinstance(study, dict) else None
        for field in ("policy", "exit_policy", "parameters"):
            value = record.pop(field)
            # Incomplete jobs do not pretend to have the completed model definition.
            record[field + "_digest"] = (
                _digest(value) if isinstance(value, dict) and value else None
            )
            if field == "policy":
                costs = (
                    {k: value.get(k) for k in ("fee_rate", "slippage_fraction")}
                    if isinstance(value, dict)
                    else {}
                )
                record["cost_digest"] = (
                    _digest(costs)
                    if costs and all(v is not None for v in costs.values())
                    else None
                )
        records.append(record)
    return build_progress(
        records,
        scope=scope,
        scope_key=scope_key,
        start_ms=start,
        as_of_ms=as_of,
        minimum_effective_pairs=minimum_effective_pairs,
        truncated=len(rows) > limit,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--minimum-effective-pairs", type=int, default=30)
    args = parser.parse_args(argv)
    import psycopg

    try:
        scope = AccountScope("BINANCE", args.account_id, "SANDBOX", "FUTURES")
        with psycopg.connect(
            os.environ["V2_REVIEW_READ_DSN"],
            connect_timeout=3,
            options="-c default_transaction_read_only=on -c statement_timeout=5000 -c lock_timeout=100",
            application_name="v2-review-progress-read-only",
        ) as conn:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            report = load_progress(
                conn,
                scope,
                days=args.days,
                limit=args.limit,
                minimum_effective_pairs=args.minimum_effective_pairs,
            )
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    except Exception:  # noqa: BLE001 - never log DSNs, account identities or DB errors
        print(
            json.dumps(
                {"error": "REVIEW_PROGRESS_UNAVAILABLE", "execution_authorized": False}
            )
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
