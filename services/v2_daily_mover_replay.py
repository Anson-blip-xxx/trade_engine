"""Read-only, bounded re-evaluation of immutable historical mover inputs.

This is the CURRENT evaluator on frozen inputs, checked against the stored
baseline reason. It is not a byte-identical historical-code replay, backtest,
admission decision, or estimate of executable profit. Missing inputs are never
replaced with current policy/context, and one bad record cannot block a report.
"""

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import asdict

from services.v2_mover_gate_shadow import evaluate_mover_gate_shadow
from services.v2_trend_entry import REJECTIONS
from services.v2_trend_entry import VERSION as EVALUATOR_VERSION
from v2_core.account_risk import AccountScope
from v2_core.directional import supports_event
from v2_core.evidence import DecisionEvidence, canonical, digest
from v2_core.research_market_sources import market_comparison_contract
from v2_core.strategy import StrategyScope
from v2_core.symbols import is_supported_symbol

VERSION = "daily-mover-frozen-replay-v1"
MAX_SAMPLES = 100
MAX_READ_SECONDS = 15
_DAY = 86_400_000
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_REASONS = REJECTIONS | {
    "CANDIDATE",
    "regime_conflict",
    "zero_volatility",
    "score_below_minimum",
    "TV_DIRECTION_CONFLICT",
}
_BASIS = "CURRENT_EVALUATOR_ON_FROZEN_INPUTS_BASELINE_CHECKED"

# Resolve only selected immutable hashes. Consumer identity binds the account,
# and the generated evidence signal_id binds the original inbound snapshot.
# No full-table scan, raw model prompt, or mutable current policy is requested.
# Large evidence is omitted before transfer; LIMIT detects duplicate joins.
_REPLAY_SQL = """
SELECT e.evidence_ref,e.config_digest,e.strategy_version,e.signal_id::text,
       d.consumer,s.source,s.environment,
       (extract(epoch FROM e.created_at)*1000)::bigint,
       (extract(epoch FROM d.created_at)*1000)::bigint,
       (extract(epoch FROM s.received_at)*1000)::bigint,
       CASE WHEN octet_length(e.config::text)+octet_length(e.snapshot::text)
                    +octet_length(s.snapshot::text)<=524288
            THEN e.config END,
       CASE WHEN octet_length(e.config::text)+octet_length(e.snapshot::text)
                    +octet_length(s.snapshot::text)<=524288
            THEN e.snapshot END,
       CASE WHEN octet_length(e.config::text)+octet_length(e.snapshot::text)
                    +octet_length(s.snapshot::text)<=524288
            THEN s.snapshot END
FROM v2_decision_evidence e
JOIN v2_strategy_decisions d ON d.evidence_ref=e.evidence_ref
  AND d.signal_id=e.signal_id AND d.consumer=ANY(%s::text[])
JOIN v2_inbound_signals s ON s.signal_id=e.signal_id
WHERE e.evidence_ref=ANY(%s::text[]) AND s.environment=%s
  AND e.created_at<=to_timestamp(%s/1000.0)
  AND d.created_at<=to_timestamp(%s/1000.0)
  AND s.received_at<=to_timestamp(%s/1000.0)
ORDER BY e.evidence_ref,d.consumer
LIMIT 41
"""


def _clock(value):
    return type(value) is int and 0 <= value < 2**53


def _hash(value):
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _seal(result):
    result["coverage"]["retryable"] = result["retryable"]
    encoded = json.dumps(
        result,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(encoded.encode()) > 512 * 1024:
        raise ValueError("BOUNDED_REPLAY_OUTPUT_REQUIRED")
    result["content_digest"] = hashlib.sha256(encoded.encode()).hexdigest()
    return result


def _envelope(report, as_of_ms):
    """Project binding fields only; never echo account IDs or arbitrary inputs."""
    report = report if isinstance(report, dict) else {}
    window = report.get("window")
    window = window if isinstance(window, dict) else {}
    evidence = report.get("account_evidence")
    evidence = evidence if isinstance(evidence, dict) else {}
    return {
        "version": VERSION,
        "status": "UNKNOWN",
        "reason": "INPUT_UNAVAILABLE",
        "basis": _BASIS,
        "evaluator_version": EVALUATOR_VERSION,
        "historical_code_identity": "NOT_PROVEN",
        "historical_program_replay": False,
        "market_comparison": market_comparison_contract(
            research_source=report.get("market_source"),
            execution_environment=report.get("account_environment"),
        ),
        "account_environment": report.get("account_environment")
        if isinstance(report.get("account_environment"), str)
        and report.get("account_environment") in {"LIVE", "SANDBOX"}
        else "UNKNOWN",
        "scope_key": report.get("scope_key")
        if _hash(report.get("scope_key"))
        else None,
        "as_of_ms": as_of_ms if _clock(as_of_ms) else None,
        "evidence_as_of_ms": evidence.get("as_of_ms")
        if _clock(evidence.get("as_of_ms"))
        else None,
        "window_start_ms": window.get("start_ms")
        if _clock(window.get("start_ms"))
        else None,
        "window_end_ms": window.get("end_ms") if _clock(window.get("end_ms")) else None,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
        "not_trade_prediction": True,
        "out_of_sample_validation": "NOT_COMPLETED",
        "cohort_scope": "SELECTED_MOVER_REJECTIONS_NOT_INDEPENDENT_TRADES",
        "volume_plan": {
            "baseline_min_volume_ratio": "1",
            "candidate_min_volume_ratio": "0.75",
        },
        "regime_plan": "UNCHANGED_REQUIRES_SEPARATE_RELATIVE_STRENGTH_STUDY",
        "coverage": {"query_complete": False, "all_inputs_available": False},
        "retryable": False,
        "summary": {
            key: 0
            for key in (
                "requested_count",
                "replayed_count",
                "baseline_matched_count",
                "baseline_mismatch_count",
                "unknown_count",
                "volume_gate_changed_count",
                "new_market_candidate_count",
                "other_gate_still_failed_count",
            )
        },
        "samples": [],
        "unknown_reasons": {},
    }


def unavailable_review(report, *, as_of_ms, reason="REPLAY_READ_UNAVAILABLE"):
    """Safe public fallback for callers; arbitrary exception text is discarded."""
    result = _envelope(report, as_of_ms)
    allowed = {
        "REPLAY_INPUT_INVALID",
        "REPLAY_READ_UNAVAILABLE",
        "REPLAY_READ_ONLY_REQUIRED",
        "REPLAY_STABLE_SNAPSHOT_REQUIRED",
        "REPLAY_QUERY_TIMEOUT",
        "REPLAY_PERMISSION_MISSING",
        "REPLAY_SCHEMA_UNAVAILABLE",
        "REPLAY_READ_BUDGET_EXCEEDED",
    }
    result["reason"] = (
        reason
        if isinstance(reason, str) and reason in allowed
        else "REPLAY_READ_UNAVAILABLE"
    )
    result["retryable"] = result["reason"] in {
        "REPLAY_READ_UNAVAILABLE",
        "REPLAY_QUERY_TIMEOUT",
        "REPLAY_PERMISSION_MISSING",
        "REPLAY_SCHEMA_UNAVAILABLE",
        "REPLAY_READ_BUDGET_EXCEEDED",
    }
    selected = report.get("gate_shadow", {}) if isinstance(report, dict) else {}
    selected = selected.get("samples") if isinstance(selected, dict) else None
    if isinstance(selected, list) and len(selected) <= MAX_SAMPLES:
        result["summary"]["requested_count"] = len(selected)
        result["summary"]["unknown_count"] = len(selected)
        if selected:
            result["unknown_reasons"] = {result["reason"]: len(selected)}
    return _seal(result)


def _selection(scope, report, as_of_ms):
    if not isinstance(scope, AccountScope) or not isinstance(report, dict):
        raise TypeError
    account = asdict(scope)
    evidence = report["account_evidence"]
    window = report["window"]
    start, end, cutoff = window["start_ms"], window["end_ms"], evidence["as_of_ms"]
    if (
        not all(_clock(x) for x in (start, end, cutoff, as_of_ms))
        or end - start != _DAY
        or (start + 28_800_000) % _DAY
        or not end <= cutoff <= as_of_ms
        or report["as_of_ms"] != as_of_ms
        or report["scope_key"] != digest(canonical(account))
        or report["account_environment"] != scope.environment
        or evidence["account_scope"] != account
        or evidence["window_start_ms"] != start
        or evidence["window_end_ms"] != end
    ):
        raise ValueError
    selected = report["gate_shadow"]["samples"]
    rows = report["rows"]
    if not isinstance(selected, list) or len(selected) > MAX_SAMPLES:
        raise ValueError
    if not isinstance(rows, list) or len(rows) > 20:
        raise ValueError
    bindings = {}
    for row in rows:
        symbol, side = row["symbol"], row["direction"]
        if not is_supported_symbol(symbol) or side not in {"BUY", "SELL"}:
            raise ValueError
        groups = row["evidence"].get("rejection_details", [])
        if not isinstance(groups, list) or len(groups) > 50:
            raise ValueError
        total = 0
        for group in groups:
            samples = group["reason_samples"]
            if not isinstance(samples, list):
                raise TypeError
            total += len(samples)
            if total > 50:
                raise ValueError
            for sample in samples:
                ref = sample.get("evidence_ref")
                if not _hash(ref):
                    continue
                binding = {
                    key: sample.get(key)
                    for key in (
                        "source",
                        "event",
                        "observed_ms",
                        "decided_ms",
                    )
                }
                binding.update(
                    symbol=symbol,
                    side=side,
                    market_sources=sample.get("market_sources"),
                )
                if ref in bindings and bindings[ref] != binding:
                    bindings[ref] = None
                else:
                    bindings[ref] = binding
    references = [
        item.get("evidence_ref") if isinstance(item, dict) else None
        for item in selected
    ]
    counts = Counter(ref for ref in references if _hash(ref))
    for ref, count in counts.items():
        if count > 1:
            bindings[ref] = None
    return references, bindings, start, end, cutoff


def _unknown(ref, reason):
    return {
        "evidence_ref": ref if _hash(ref) else None,
        "status": "UNKNOWN",
        "reason": reason,
    }


def _record(record, binding, scope, *, start, end, cutoff, evaluator=None):
    if len(record) != 13 or binding is None:
        raise ValueError
    (
        ref,
        config_hash,
        version,
        signal_id,
        consumer,
        source,
        environment,
        e_at,
        d_at,
        s_at,
        config,
        snapshot,
        signal,
    ) = record
    if not all(isinstance(v, dict) for v in (config, snapshot, signal)):
        return None, "FROZEN_INPUTS_MISSING_OR_TOO_LARGE"
    account = asdict(scope)
    strategy = "S6" if binding["side"] == "BUY" else "S8"
    if (
        consumer != StrategyScope(**account, producer=strategy.lower()).consumer
        or environment != scope.environment
        or source not in {"s3", "tv_bridge"}
        or source != binding["source"]
        or config.get("source") != source
        or config.get("account_scope") != account
        or config.get("strategy") != strategy
        or snapshot.get("signal_id") != signal_id
        or snapshot.get("source") != source
        or snapshot.get("symbol") != binding["symbol"]
        or signal.get("symbol") != binding["symbol"]
        or signal.get("signal") != binding["event"]
        or not supports_event(strategy, signal.get("signal"))
        or any(not _clock(t) or t > cutoff for t in (e_at, d_at, s_at))
        or d_at != binding["decided_ms"]
    ):
        return None, "FROZEN_BINDING_MISMATCH"
    observed, decided = snapshot.get("observed_at"), snapshot.get("decided_at")
    if (
        not _clock(observed)
        or not _clock(decided)
        or not start <= observed < end
        or not observed <= decided <= cutoff
        or signal.get("observed_at") != observed
        or binding["observed_ms"] != observed
        or not observed <= s_at <= d_at
        or not decided <= e_at <= d_at
    ):
        return None, "FROZEN_TIME_MISMATCH"
    # Validate both stored hashes, including the signal ID and all context.
    config_json, snapshot_json, signal_json = (
        canonical(value) for value in (config, snapshot, signal)
    )
    if (
        sum(len(value.encode()) for value in (config_json, snapshot_json, signal_json))
        > 524288
    ):
        return None, "FROZEN_INPUTS_MISSING_OR_TOO_LARGE"
    proof = DecisionEvidence(version, config_json, snapshot_json)
    if proof.config_digest != config_hash or proof.evidence_ref != ref:
        return None, "FROZEN_DIGEST_MISMATCH"
    features = snapshot.get("features", {})
    context = features.get("context")
    if not isinstance(context, dict):
        return None, "FROZEN_CONTEXT_MISSING"
    if (
        context.get("account_scope") != account
        or context.get("environment") != scope.environment
        or not _clock(context.get("assembled_at"))
        or not observed <= context["assembled_at"] <= decided
        or features.get("signal") != signal.get("features")
    ):
        return None, "FROZEN_CONTEXT_BINDING_MISMATCH"
    market_reason = features.get("evaluation", {}).get("market_plan", {}).get("reason")
    if not isinstance(market_reason, str) or market_reason not in _REASONS:
        return None, "FROZEN_BASELINE_REASON_UNAVAILABLE"
    sample = {
        "evidence_ref": ref,
        "config_digest": config_hash,
        "config": config,
        "context": context,
        "signal": signal,
        "symbol": binding["symbol"],
        "environment": scope.environment,
        "require_market_environment": True,
        "market_sources": binding["market_sources"],
    }
    # Research adapters may reuse the same immutable provenance checks with a
    # different pure projection. Raw objects never enter persisted reports.
    if evaluator is not None:
        return evaluator(sample, market_reason), None
    comparison = evaluate_mover_gate_shadow([sample], environment=scope.environment)[
        "samples"
    ][0]
    if comparison["mode"] != "FULL_FROZEN_REPLAY":
        provenance = comparison.get("market_provenance")
        if isinstance(provenance, dict) and provenance["replay_blocked"]:
            return None, provenance["reason"]
        if comparison["volume"]["reason"] == "INCOMPLETE_FROZEN_POLICY":
            return None, "INCOMPLETE_FROZEN_POLICY"
        return None, "FROZEN_INPUTS_NOT_REPLAYABLE"
    volume = comparison["volume"]
    baseline, candidate = (
        volume["baseline_market_reason"],
        volume["candidate_market_reason"],
    )
    if baseline not in _REASONS or candidate not in _REASONS:
        return None, "CURRENT_EVALUATOR_REASON_UNRECOGNIZED"
    return {
        "evidence_ref": ref,
        "symbol": binding["symbol"],
        "side": binding["side"],
        "source": source,
        "status": "BASELINE_MATCHED"
        if baseline == market_reason
        else "BASELINE_MISMATCH",
        "recorded_market_reason": market_reason,
        "baseline_market_reason": baseline,
        "candidate_market_reason": candidate,
        "volume_gate_changed": volume["gate_changed"] is True,
        "new_market_candidate": baseline == market_reason
        and baseline != "CANDIDATE"
        and candidate == "CANDIDATE",
        "other_gate_still_failed": volume["other_gate_status"] == "STILL_FAILS",
        "config_digest": config_hash,
        "policy_compatibility": comparison["policy_compatibility"],
        "input_digest": comparison["input_digest"],
        "strategy_version_digest": digest(version),
        "historical_code_identity": "NOT_PROVEN",
        "market_provenance": comparison["market_provenance"],
    }, None


def load_frozen_replay_review(conn, scope, report, *, as_of_ms):
    """Read <=100 selected hashes inside a short stable read-only snapshot.

    One nested transaction/savepoint confines schema, permission and timeout
    errors. No writes, commits, network calls, current-policy substitution,
    order functions, model calls or raw input export occur here.
    """
    deadline = time.monotonic() + MAX_READ_SECONDS
    try:
        refs, bindings, start, end, cutoff = _selection(scope, report, as_of_ms)
    except (TypeError, ValueError, KeyError, AttributeError, RecursionError):
        return unavailable_review(
            report, as_of_ms=as_of_ms, reason="REPLAY_INPUT_INVALID"
        )
    result = _envelope(report, as_of_ms)
    result["summary"]["requested_count"] = len(refs)
    requested = sorted(
        {ref for ref in refs if _hash(ref) and bindings.get(ref) is not None}
    )
    consumers = [
        StrategyScope(**asdict(scope), producer=p).consumer for p in ("s6", "s8")
    ]
    resolved = {}
    failure = None
    try:
        with conn.transaction():
            if conn.execute("SHOW transaction_read_only").fetchone() != ("on",):
                return unavailable_review(
                    report, as_of_ms=as_of_ms, reason="REPLAY_READ_ONLY_REQUIRED"
                )
            if conn.execute("SHOW transaction_isolation").fetchone() not in {
                ("repeatable read",),
                ("serializable",),
            }:
                return unavailable_review(
                    report, as_of_ms=as_of_ms, reason="REPLAY_STABLE_SNAPSHOT_REQUIRED"
                )
            conn.execute("SET LOCAL statement_timeout='3s'")
            conn.execute("SET LOCAL lock_timeout='100ms'")
            # Bound peak memory as well as SQL rows. Never retain all source
            # blobs for 100 samples; only public summaries survive each batch.
            for offset in range(0, len(requested), 20):
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms < 1:
                    failure = "REPLAY_READ_BUDGET_EXCEEDED"
                    break
                conn.execute(
                    f"SET LOCAL statement_timeout='{min(3000, remaining_ms)}ms'"
                )
                batch = requested[offset : offset + 20]
                raw = conn.execute(
                    _REPLAY_SQL,
                    (consumers, batch, scope.environment, cutoff, cutoff, cutoff),
                ).fetchall()
                if len(raw) > 40:
                    raise ValueError("BOUNDED_RESULT_REQUIRED")
                by_ref = {}
                for record in raw:
                    if (
                        not isinstance(record, (tuple, list))
                        or len(record) != 13
                        or record[0] not in batch
                    ):
                        raise ValueError("UNBOUND_RESULT")
                    by_ref.setdefault(record[0], []).append(record)
                for ref in batch:
                    if time.monotonic() >= deadline:
                        failure = "REPLAY_READ_BUDGET_EXCEEDED"
                        break
                    if len(by_ref.get(ref, [])) != 1:
                        resolved[ref] = _unknown(
                            ref, "FROZEN_RECORD_MISSING_OR_CONFLICTING"
                        )
                        continue
                    try:
                        row, reason = _record(
                            by_ref[ref][0],
                            bindings[ref],
                            scope,
                            start=start,
                            end=end,
                            cutoff=cutoff,
                        )
                        resolved[ref] = row or _unknown(ref, reason)
                    except Exception:  # noqa: BLE001 - malformed immutable data is not public error text
                        resolved[ref] = _unknown(ref, "INVALID_FROZEN_INPUT")
                # Drop raw objects before the next bounded query.
                by_ref.clear()
                raw.clear()
                if failure:
                    break
    except Exception as exc:  # noqa: BLE001 - optional research must never block daily review
        failure = {
            "42501": "REPLAY_PERMISSION_MISSING",
            "42P01": "REPLAY_SCHEMA_UNAVAILABLE",
            "57014": "REPLAY_QUERY_TIMEOUT",
        }.get(getattr(exc, "sqlstate", None), "REPLAY_READ_UNAVAILABLE")
    result["coverage"]["query_complete"] = failure is None
    result["retryable"] = failure is not None
    for ref in refs:
        if not _hash(ref) or bindings.get(ref) is None:
            row = _unknown(ref, "SELECTED_REFERENCE_MISSING_OR_CONFLICTING")
        else:
            row = resolved.get(ref) or _unknown(
                ref, failure or "FROZEN_RECORD_MISSING_OR_CONFLICTING"
            )
        result["samples"].append(row)
    summary = result["summary"]
    for row in result["samples"]:
        status = row["status"]
        if status == "UNKNOWN":
            summary["unknown_count"] += 1
        else:
            summary["replayed_count"] += 1
            summary[
                "baseline_matched_count"
                if status == "BASELINE_MATCHED"
                else "baseline_mismatch_count"
            ] += 1
            if status == "BASELINE_MATCHED":
                for name, field in (
                    ("volume_gate_changed_count", "volume_gate_changed"),
                    ("new_market_candidate_count", "new_market_candidate"),
                    ("other_gate_still_failed_count", "other_gate_still_failed"),
                ):
                    summary[name] += row[field]
    result["unknown_reasons"] = dict(
        sorted(
            Counter(
                row["reason"] for row in result["samples"] if row["status"] == "UNKNOWN"
            ).items()
        )
    )
    result["coverage"]["all_inputs_available"] = (
        bool(refs) and summary["unknown_count"] == 0
    )
    result["status"] = (
        "AVAILABLE"
        if refs and summary["baseline_matched_count"] == len(refs)
        else "PARTIAL"
        if summary["replayed_count"]
        else "UNKNOWN"
    )
    result["reason"] = failure or (
        "FROZEN_INPUTS_BASELINE_CHECKED"
        if summary["replayed_count"]
        else "NO_COMPARABLE_FROZEN_INPUTS"
    )
    return _seal(result)
