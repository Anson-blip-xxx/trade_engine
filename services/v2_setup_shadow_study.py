"""Bounded account decision samples, independent of ex-post mover rankings.

Hourly convenience sampling is deliberately not a census or a random sample.
This independent research never admits orders, changes policy or predicts PnL.
"""

import json
import re
import time
from collections import Counter
from dataclasses import asdict

from services.v2_daily_mover_replay import _REPLAY_SQL, _record
from services.v2_mover_gate_shadow import _environment
from services.v2_setup_shadow import evaluate_setup_shadow
from v2_core.account_risk import AccountScope
from v2_core.directional import supports_event
from v2_core.evidence import canonical, digest
from v2_core.strategy import StrategyScope
from v2_core.symbols import is_supported_symbol

VERSION = "setup-shadow-study-v1"
HOUR, DAY, BAR = 3_600_000, 86_400_000, 900_000
MAX_READ_SECONDS = 30
PLAN = {
    "version": "setup-volume-plan-v1",
    "registration_boundary_ms": 1_793_808_000_000,  # 2026-11-05 00:00 UTC+8
    "forward_window_days": 14,
    "baseline_min_volume_ratio": "1",
    "alternatives": {"BREAKOUT_VOLUME075": "0.75", "PULLBACK_VOLUME075": "0.75"},
    "other_policy": "UNCHANGED_FROZEN_VALUES",
    "sampling": "FIRST32_DECISIONS_PER_HOUR_FIRST4_DISTINCT_CLOSED_BAR_OPPORTUNITIES",
    "decision_tie_order": "CREATED_AT_ASC_DECISION_ID_DESC",
    "scope_discovery": "EXISTING_DAILY_RESEARCH_ACCOUNTS_NOT_ALL_SYSTEM_ACCOUNTS",
    "opportunity": "ACCOUNT_ENV_SYMBOL_SIDE_CLOSED_15M_BAR_CROSS_SOURCE",
    "path_sampling": "FIXED_EQUISPACED_MAX12_BEFORE_VALIDITY_OR_CANDIDATE_FILTER",
    "horizons_minutes": [30, 60, 240],
    "selection_uses_rankings_or_future_outcomes": False,
    "automatic_parameter_updates": False,
    "promotion_authorized": False,
}
PLAN_DIGEST = digest(canonical(PLAN))
_HASH = re.compile(r"[0-9a-f]{64}\Z")

# The indexed decision scan happens BEFORE all JSON projections. The 33rd row
# is a sentinel only. Each hour contributes at most four preselected references.
_SELECT_SQL = """WITH hours AS (
  SELECT generate_series(%s::bigint,%s::bigint-3600000,3600000) hour_ms)
SELECT h.hour_ms,d.consumer,d.evidence_ref,
  (extract(epoch FROM d.created_at)*1000)::bigint,
  s.source,s.environment,s.snapshot->>'symbol',s.snapshot->>'signal',
  s.snapshot->>'observed_at',
  e.snapshot#>>'{features,context,directional,market,trend_structure,last_closed_at_ms}'
FROM hours h CROSS JOIN LATERAL (
  SELECT one_consumer.* FROM unnest(%s::text[]) consumers(consumer)
  CROSS JOIN LATERAL (
    SELECT d.consumer,d.evidence_ref,d.signal_id,d.created_at,d.decision_id
    FROM v2_strategy_decisions d
    WHERE d.consumer=consumers.consumer
      AND d.created_at>=to_timestamp(h.hour_ms/1000.0)
      AND d.created_at<to_timestamp((h.hour_ms+3600000)/1000.0)
      AND d.created_at<=to_timestamp(%s/1000.0)
    ORDER BY d.created_at ASC,d.decision_id DESC LIMIT 33
  ) one_consumer
  ORDER BY created_at ASC,decision_id DESC LIMIT 33
) d LEFT JOIN v2_inbound_signals s USING(signal_id)
LEFT JOIN v2_decision_evidence e ON e.evidence_ref=d.evidence_ref
ORDER BY h.hour_ms,d.created_at ASC,d.decision_id DESC LIMIT 792"""


def _seal(value):
    result = {k: v for k, v in value.items() if k != "content_digest"}
    encoded = json.dumps(
        result,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode()) > 512 * 1024:
        raise ValueError("SETUP_STUDY_OUTPUT_LIMIT")
    return {**result, "content_digest": digest(encoded)}


def _stamp(value):
    return type(value) is int and 0 <= value < 2**53


def _integer(value):
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}", value):
        value = int(value)
    if not _stamp(value):
        raise ValueError("INVALID_TIME")
    return value


def _envelope(scope, start, end, cutoff):
    boundary = PLAN["registration_boundary_ms"]
    return {
        "version": VERSION,
        "status": "UNKNOWN",
        "reason": "NOT_EVALUATED",
        "scope_key": digest(canonical(asdict(scope))),
        "account_environment": scope.environment,
        "window_start_ms": start,
        "window_end_ms": end,
        "as_of_ms": cutoff,
        "plan": PLAN,
        "plan_digest": PLAN_DIGEST,
        "evaluation_partition": "PRE_REGISTRATION_EXPLORATORY"
        if start < boundary
        else "FIXED_FORWARD_WINDOW"
        if start < boundary + 14 * DAY
        else "AFTER_FIXED_WINDOW",
        "historical_code_identity": "NOT_PROVEN",
        "not_trade_prediction": True,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
        "coverage": {
            "query_complete": False,
            "retryable": False,
            "population_complete": False,
            "hourly_decision_cap": 32,
            "hourly_opportunity_cap": 4,
            "selected_reference_limit": 96,
        },
        "selection": None,
        "input_digest": None,
        "samples": [],
        "cohorts": [],
        "summary": {},
        "limitations": [
            "BOUNDED_TIME_STRATIFIED_CONVENIENCE_SAMPLE_NOT_RANDOM_OR_CENSUS",
            "OVERLAPPING_MARKET_WINDOWS_NOT_INDEPENDENT_TRADES",
            "CURRENT_EVALUATOR_BASELINE_CHECK_NOT_HISTORICAL_PROGRAM_REPLAY",
            "MARKET_CANDIDATE_NOT_FULL_ADMISSION_OR_PROFIT",
            "NO_AUTOMATIC_PARAMETER_SELECTION_OR_DEPLOYMENT",
        ],
    }


def _read(conn, deadline, operation):
    left = min(3000, int((deadline - time.monotonic()) * 1000))
    if left <= 0:
        return None, "READ_BUDGET_EXHAUSTED"
    try:
        with conn.transaction():
            if conn.execute("SHOW transaction_read_only").fetchone() != ("on",):
                return None, "READ_ONLY_REQUIRED"
            if conn.execute("SHOW transaction_isolation").fetchone() not in {
                ("repeatable read",),
                ("serializable",),
            }:
                return None, "STABLE_SNAPSHOT_REQUIRED"
            conn.execute(f"SET LOCAL statement_timeout='{left}ms'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            return operation(), None
    except Exception as exc:  # noqa: BLE001 - no raw DB details in research output
        return None, {
            "57014": "QUERY_TIMEOUT",
            "42501": "READ_PERMISSION_MISSING",
            "42P01": "READ_SCHEMA_UNAVAILABLE",
            "55P03": "QUERY_LOCK_TIMEOUT",
        }.get(getattr(exc, "sqlstate", None), "READ_FAILED")


def _selected_metadata(rows, scope, start, end):
    consumers = {
        StrategyScope(**asdict(scope), producer=p).consumer: side
        for p, side in (("s6", "BUY"), ("s8", "SELL"))
    }
    hourly = {start + i * HOUR: [] for i in range(24)}
    if len(rows) > 792:
        raise ValueError("SELECTION_LIMIT")
    for row in rows:
        if len(row) != 10 or row[0] not in hourly:
            raise ValueError("SELECTION_ROW_INVALID")
        hourly[row[0]].append(row)
    selected, seen, hours, excluded = [], set(), [], Counter()
    for hour, batch in hourly.items():
        if len(batch) > 33:
            raise ValueError("SELECTION_LIMIT")
        chosen, duplicates = 0, 0
        # SQL establishes the order, including decision_id ties. Never reorder
        # using eligibility, PnL, ranking membership or later path availability.
        for (
            _,
            consumer,
            ref,
            decided,
            source,
            env,
            symbol,
            event,
            observed,
            bar,
        ) in batch[:32]:
            try:
                side = consumers[consumer]
                observed, bar = _integer(observed), _integer(bar)
                if (
                    not _stamp(decided)
                    or not hour <= decided < hour + HOUR
                    or not 0 <= bar <= observed <= decided
                    or bar % BAR
                    or not isinstance(ref, str)
                    or _HASH.fullmatch(ref) is None
                    or env != scope.environment
                    or source not in {"s3", "tv_bridge"}
                    or not is_supported_symbol(symbol)
                    or not supports_event("S6" if side == "BUY" else "S8", event)
                ):
                    raise ValueError("SELECTION_BINDING_INVALID")
            except (ValueError, TypeError, KeyError):
                excluded["MISSING_OR_INVALID_METADATA"] += 1
                continue
            opportunity = digest(
                canonical(
                    {
                        "scope_key": digest(canonical(asdict(scope))),
                        "symbol": symbol,
                        "side": side,
                        "bar_close_ms": bar,
                    }
                )
            )
            if opportunity in seen:
                duplicates += 1
                continue
            if chosen >= 4:
                excluded["HOURLY_SELECTION_LIMIT"] += 1
                continue
            seen.add(opportunity)
            selected.append(
                {
                    "opportunity_ref": opportunity,
                    "evidence_ref": ref,
                    "symbol": symbol,
                    "side": side,
                    "source": source,
                    "event": event,
                    "observed_ms": observed,
                    "decided_ms": decided,
                    "bar_close_ms": bar,
                    "environment": env,
                }
            )
            chosen += 1
        hours.append(
            {
                "hour_start_ms": hour,
                "observed_metadata_count": len(batch),
                "scan_truncated": len(batch) == 33,
                "selected_count": chosen,
                "duplicates_removed": duplicates,
            }
        )
    return {
        "selected_at_ms": None,
        "rows": selected,
        "hours": hours,
        "excluded_reasons": dict(excluded),
    }


def _prior_selection(prior, result):
    if (
        not isinstance(prior, dict)
        or prior.get("version") != VERSION
        or prior.get("plan_digest") != PLAN_DIGEST
    ):
        return None
    for key in ("scope_key", "account_environment", "window_start_ms", "window_end_ms"):
        if prior.get(key) != result[key]:
            return None
    if prior.get("content_digest") != _seal(prior)["content_digest"]:
        return None
    if prior.get("plan") != PLAN or any(
        prior.get(k) is not False
        for k in (
            "execution_authorized",
            "automatic_parameter_updates",
            "promotion_authorized",
        )
    ):
        return None
    selection = prior.get("selection")
    if (
        not isinstance(selection, dict)
        or not isinstance(selection.get("rows"), list)
        or len(selection["rows"]) > 96
        or not _stamp(selection.get("selected_at_ms"))
        or not result["window_end_ms"]
        <= selection["selected_at_ms"]
        <= result["as_of_ms"]
        or prior.get("input_digest") != digest(canonical(selection))
    ):
        return None
    expected_keys = {"selected_at_ms", "rows", "hours", "excluded_reasons"}
    row_keys = {
        "opportunity_ref",
        "evidence_ref",
        "symbol",
        "side",
        "source",
        "event",
        "observed_ms",
        "decided_ms",
        "bar_close_ms",
        "environment",
    }
    if set(selection) != expected_keys:
        return None
    counts, refs = Counter(), set()
    for row in selection["rows"]:
        if not isinstance(row, dict) or set(row) != row_keys:
            return None
        if (
            any(
                not _stamp(row[key])
                for key in ("observed_ms", "decided_ms", "bar_close_ms")
            )
            or not result["window_start_ms"]
            <= row["decided_ms"]
            < result["window_end_ms"]
            or not row["bar_close_ms"] <= row["observed_ms"] <= row["decided_ms"]
            or row["bar_close_ms"] % BAR
            or row["environment"] != result["account_environment"]
            or row["side"] not in {"BUY", "SELL"}
            or row["source"] not in {"s3", "tv_bridge"}
            or not is_supported_symbol(row["symbol"])
            or not isinstance(row["evidence_ref"], str)
            or _HASH.fullmatch(row["evidence_ref"]) is None
            or not supports_event("S6" if row["side"] == "BUY" else "S8", row["event"])
        ):
            return None
        expected = digest(
            canonical(
                {
                    "scope_key": result["scope_key"],
                    "symbol": row["symbol"],
                    "side": row["side"],
                    "bar_close_ms": row["bar_close_ms"],
                }
            )
        )
        if row["opportunity_ref"] != expected or expected in refs:
            return None
        refs.add(expected)
        counts[(row["decided_ms"] - result["window_start_ms"]) // HOUR] += 1
    hours = selection["hours"]
    exclusions = selection["excluded_reasons"]
    if (
        any(count > 4 for count in counts.values())
        or not isinstance(hours, list)
        or len(hours) != 24
        or not isinstance(exclusions, dict)
        or set(exclusions) - {"MISSING_OR_INVALID_METADATA", "HOURLY_SELECTION_LIMIT"}
        or any(type(n) is not int or not 0 <= n <= 792 for n in exclusions.values())
    ):
        return None
    for offset, hour in enumerate(hours):
        if (
            not isinstance(hour, dict)
            or set(hour)
            != {
                "hour_start_ms",
                "observed_metadata_count",
                "scan_truncated",
                "selected_count",
                "duplicates_removed",
            }
            or hour["hour_start_ms"] != result["window_start_ms"] + offset * HOUR
            or any(
                type(hour[k]) is not int or not 0 <= hour[k] <= 33
                for k in (
                    "observed_metadata_count",
                    "selected_count",
                    "duplicates_removed",
                )
            )
            or hour["scan_truncated"] is not (hour["observed_metadata_count"] == 33)
            or hour["selected_count"] != counts.get(offset, 0)
        ):
            return None
    return selection


def _project(sample, recorded):
    _environment(sample, sample["environment"])
    result = evaluate_setup_shadow(
        sample["signal"],
        sample["context"],
        sample["config"],
        environment=sample["environment"],
        evidence_ref=sample["evidence_ref"],
        config_digest=sample["config_digest"],
    )
    if result["status"] != "READY":
        return result
    result["status"] = (
        "BASELINE_MATCHED"
        if result["baseline_market_reason"] == recorded
        else "BASELINE_MISMATCH"
    )
    result["recorded_market_reason"] = recorded
    if result["status"] == "BASELINE_MISMATCH":
        result["alternatives"] = {}
    return result


def _sources(snapshot):
    context = snapshot.get("features", {}).get("context", {})
    market = context.get("directional", {}).get("market", {}).get("input_evidence", {})
    s3 = context.get("sources", {}).get("s3", {})
    return {
        "market_environment": market.get("environment"),
        "market_source": market.get("source"),
        "market_host": market.get("source_host"),
        "s3_environment": s3.get("environment"),
        "s3_source": s3.get("source"),
    }


def _finish(result):
    rows = result["samples"]
    valid = [r for r in rows if r["status"] == "BASELINE_MATCHED"]
    result["summary"] = {
        "selected_count": len(rows),
        "baseline_matched_count": len(valid),
        "baseline_mismatch_count": sum(
            r["status"] == "BASELINE_MISMATCH" for r in rows
        ),
        "unknown_count": sum(r["status"] == "UNKNOWN" for r in rows),
        "baseline_market_candidates": sum(
            r["baseline_market_reason"] == "CANDIDATE" for r in valid
        ),
        "alternatives": {},
    }
    groups = {}
    for row in valid:
        key = (row["side"], row["regime"], row["setup"])
        group = groups.setdefault(
            key,
            {
                "side": key[0],
                "regime": key[1],
                "setup": key[2],
                "opportunity_count": 0,
                "baseline_market_candidates": 0,
                "alternatives": {},
            },
        )
        group["opportunity_count"] += 1
        group["baseline_market_candidates"] += (
            row["baseline_market_reason"] == "CANDIDATE"
        )
        for name in PLAN["alternatives"]:
            candidate = row["alternatives"][name]["market_reason"] == "CANDIDATE"
            new = candidate and row["baseline_market_reason"] != "CANDIDATE"
            for item in (group, result["summary"]):
                values = item["alternatives"].setdefault(
                    name, {"market_candidate_count": 0, "new_market_candidate_count": 0}
                )
                values["market_candidate_count"] += candidate
                values["new_market_candidate_count"] += new
    result["cohorts"] = [groups[key] for key in sorted(groups)]
    result["unknown_reasons"] = dict(
        Counter(r.get("reason", "UNKNOWN") for r in rows if r["status"] == "UNKNOWN")
    )
    result["status"] = (
        "AVAILABLE"
        if rows and len(valid) == len(rows)
        else "PARTIAL"
        if valid
        else "UNKNOWN"
    )
    if result["reason"] == "NOT_EVALUATED":
        result["reason"] = (
            "BOUNDED_SAMPLE_EVALUATED" if valid else "NO_COMPARABLE_OPPORTUNITIES"
        )
    return _seal(result)


def load_setup_study(conn, scope, *, report, as_of_ms, prior=None):
    """Sample by decision day, freeze selection, then evaluate <=96 refs."""
    if not isinstance(scope, AccountScope):
        raise TypeError("SETUP_SCOPE_REQUIRED")
    start, end = report["window"]["start_ms"], report["window"]["end_ms"]
    result = _envelope(scope, start, end, as_of_ms)
    if (
        not all(_stamp(x) for x in (start, end, as_of_ms))
        or end - start != DAY
        or (start + 28_800_000) % DAY
        or end > as_of_ms
        or report.get("scope_key") != result["scope_key"]
        or report.get("account_environment") != scope.environment
    ):
        result["reason"] = "INVALID_STUDY_BINDING"
        return _finish(result)
    deadline = time.monotonic() + MAX_READ_SECONDS
    consumers = [
        StrategyScope(**asdict(scope), producer=p).consumer for p in ("s6", "s8")
    ]
    try:
        selection = _prior_selection(prior, result)
    except (
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
        OverflowError,
    ):
        selection = None
    existing_freeze = (
        isinstance(prior, dict)
        and prior.get("version") == VERSION
        and prior.get("plan_digest") == PLAN_DIGEST
        and all(
            prior.get(k) == result[k]
            for k in (
                "scope_key",
                "account_environment",
                "window_start_ms",
                "window_end_ms",
            )
        )
        and (
            prior.get("selection") is not None
            or prior.get("selection_freeze_failed") is True
        )
    )
    if selection is None and existing_freeze:
        result.update(reason="INVALID_PRIOR_SELECTION", selection_freeze_failed=True)
        return _finish(result)
    if selection is None:
        metadata, error = _read(
            conn,
            deadline,
            lambda: conn.execute(
                _SELECT_SQL, (start, end, consumers, as_of_ms)
            ).fetchall(),
        )
        if error:
            result["reason"] = error
            result["coverage"]["retryable"] = error not in {
                "READ_ONLY_REQUIRED",
                "STABLE_SNAPSHOT_REQUIRED",
            }
            return _finish(result)
        try:
            selection = _selected_metadata(metadata, scope, start, end)
        except (ValueError, TypeError, KeyError, IndexError):
            result["reason"] = "INVALID_SELECTION_METADATA"
            return _finish(result)
        selection["selected_at_ms"] = as_of_ms
    result["selection"] = selection
    result["input_digest"] = digest(canonical(selection))
    result["coverage"]["query_complete"] = True
    selected = selection["rows"]
    for offset in range(0, len(selected), 20):
        batch = selected[offset : offset + 20]
        refs = [row["evidence_ref"] for row in batch]
        raw, error = _read(
            conn,
            deadline,
            lambda refs=refs: conn.execute(
                _REPLAY_SQL,
                (consumers, refs, scope.environment, as_of_ms, as_of_ms, as_of_ms),
            ).fetchall(),
        )
        by_ref = {}
        if not error:
            if len(raw) > 40:
                error = "FROZEN_RESULT_LIMIT"
            else:
                for record in raw:
                    if len(record) != 13 or record[0] not in refs:
                        error = "FROZEN_RESULT_INVALID"
                        break
                    by_ref.setdefault(record[0], []).append(record)
        if error:
            result["coverage"].update(query_complete=False, retryable=True)
        for meta in batch:
            row = {
                **meta,
                "status": "UNKNOWN",
                "reason": error or "FROZEN_RECORD_MISSING_OR_CONFLICTING",
            }
            try:
                if time.monotonic() >= deadline:
                    row["reason"] = "READ_BUDGET_EXHAUSTED"
                    result["coverage"].update(query_complete=False, retryable=True)
                elif not error and len(by_ref.get(meta["evidence_ref"], [])) == 1:
                    record = by_ref[meta["evidence_ref"]][0]
                    value, reason = _record(
                        record,
                        {**meta, "market_sources": _sources(record[11])},
                        scope,
                        start=0,
                        end=end,
                        cutoff=as_of_ms,
                        evaluator=_project,
                    )
                    if value is not None:
                        if value.get("status") == "UNKNOWN":
                            row["reason"] = value["reason"]
                        elif value.get("bar_close_ms") != meta["bar_close_ms"]:
                            row["reason"] = "FROZEN_BAR_BINDING_MISMATCH"
                        else:
                            row.update(value)
                            row["decided_ms"] = meta["decided_ms"]
                    else:
                        row["reason"] = reason
            except Exception:  # noqa: BLE001 - never expose malformed frozen objects
                row["reason"] = "INVALID_FROZEN_INPUT"
            result["samples"].append(row)
        by_ref.clear()
        if raw is not None:
            raw.clear()
    return _finish(result)
