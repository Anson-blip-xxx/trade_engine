"""Pure, evidence-bound migration of an identity-less legacy queue alarm.

This module neither reads a database nor clears an alarm. The caller must load
the exact immutable legacy report, collect the proof for the same account/lane
and atomically CAS the returned payload against the current version it read.
A concurrent monitoring update must make that CAS fail, never be overwritten.
"""

import re
from copy import deepcopy
from uuid import UUID

MAX_TRACKED_SIGNALS = 2002
_GAP = "historical_recovery_unproven_since_ms"
_SWEEPS = "historical_recovery_sweeps"
_LANES = frozenset(
    f"{producer}:{source}"
    for producer in ("s6", "s8")
    for source in ("s3", "tv_bridge", "quant")
)


def _require(condition):
    if not condition:
        # Do not include arbitrary report values or account identities.
        raise ValueError("LEGACY_QUEUE_COVERAGE_PROOF_INVALID")


def _integer(value, *, maximum=2**63 - 1):
    _require(type(value) is int and 0 <= value <= maximum)
    return value


def _dictionary(value):
    _require(type(value) is dict)
    return value


def _ids(value):
    _require(type(value) in (list, tuple) and len(value) <= MAX_TRACKED_SIGNALS)
    result = []
    for item in value:
        _require(type(item) is str)
        try:
            valid = str(UUID(item)) == item
        except (ValueError, AttributeError):
            valid = False
        _require(valid)
        result.append(item)
    # A complete SQL result is a set, not repeated values hiding a lost key.
    _require(len(result) == len(set(result)))
    return result


def _no_tracking_loss(entry):
    _require("tracking_gap_reason" not in entry)
    _require("tracking_overflow" not in entry or entry["tracking_overflow"] is False)


def migrate_legacy_queue_gap(
    current_payload,
    legacy_report,
    *,
    lane,
    proof_result,
    proof_from_ms,
    proof_cutoff_ms,
    now_ms,
):
    """Replace only a proven legacy coverage gap with bounded exact IDs.

    ``proof_result`` is the complete result of ``read_signal_queue``; its
    explicit recent window must cover the old alarm's oldest signal through
    the exact legacy observation. Ready tasks may add older identities.
    Retain every current tracked identity even if absent from this older proof.
    Only a subsequent monitoring pass may recheck them and clear any alarm.
    """
    current = _dictionary(current_payload)
    legacy = _dictionary(legacy_report)
    _require(type(lane) is str and lane in _LANES)
    now = _integer(now_ms)
    lower, cutoff = _integer(proof_from_ms), _integer(proof_cutoff_ms)
    _require(lower <= cutoff <= now)
    scope = _dictionary(current.get("account_scope"))
    _require(bool(scope) and scope == _dictionary(legacy.get("account_scope")))
    legacy_at = _integer(legacy.get("observed_at_ms"))
    current_at = _integer(current.get("observed_at_ms"))
    _require(legacy_at == cutoff and legacy_at <= current_at <= now)

    diagnostics = _dictionary(current.get("queue_diagnostics"))
    entry = _dictionary(diagnostics.get(lane))
    _require(_integer(entry.get(_GAP)) == legacy_at)
    _require(entry.get("status") == "UNKNOWN")
    _no_tracking_loss(entry)
    current_ids = _ids(entry.get("lag_signal_ids"))
    _require("legacy_coverage_proof" not in entry)

    legacy_diagnostics = _dictionary(legacy.get("queue_diagnostics", {}))
    legacy_entry = _dictionary(legacy_diagnostics.get(lane, {}))
    _require("lag_signal_ids" not in legacy_entry)
    _no_tracking_loss(legacy_entry)
    legacy_findings = _dictionary(legacy.get("findings"))
    legacy_lag = _dictionary(
        _dictionary(legacy_findings.get("SIGNAL_CONSUMPTION_LAG")).get(lane)
    )
    count = _integer(legacy_lag.get("count"))
    oldest = _integer(legacy_lag.get("oldest_received_at_ms"))
    _require(count > 0 and lower <= oldest <= cutoff)

    proof = _dictionary(proof_result)
    observed = _dictionary(proof.get("diagnostics"))
    _require(observed.get("status") == "COMPLETE")
    _require(observed.get("reason") == "BOUNDED_HOT_QUEUE_OBSERVED")
    _require(observed.get("scope") == "VALIDATED_READY_AND_RECENT_INBOX")
    _no_tracking_loss(observed)
    _require("failure_type" not in observed)
    for flag in ("capped", "truncated", "limit_reached"):
        _require(flag not in observed or observed[flag] is False)
    _require(_integer(observed.get("recent_from_ms")) == lower)
    _require(_integer(observed.get("recent_to_ms")) == cutoff)
    limit = _integer(observed.get("limit_per_source"), maximum=10000)
    _require(limit > 0)
    ready = _integer(observed.get("ready_keys"), maximum=limit)
    recent = _integer(observed.get("recent_keys"), maximum=limit)
    budget = _integer(observed.get("budget_ms"), maximum=5000)
    _require(budget > 0)
    elapsed = _integer(observed.get("query_ms"), maximum=5000)
    _require(elapsed <= budget)
    proof_ids = _ids(observed.get("lag_signal_ids"))

    row = proof.get("row")
    _require(type(row) in (list, tuple) and len(row) == 14)
    lag_count = _integer(row[0], maximum=MAX_TRACKED_SIGNALS)
    _require(lag_count == len(proof_ids))
    if lag_count:
        _require(_integer(row[1]) <= now)
    else:
        _require(row[1] is None)
    for number in row[2:13]:
        _integer(number, maximum=2 * limit + MAX_TRACKED_SIGNALS)
    # A former task error could have cleared without a receipt, changing an
    # old actionable item into today's non-actionable classification. Do not
    # use that reclassification as proof that the legacy lag was consumed.
    _require(row[3] == 0)
    codes = _dictionary(row[13])
    _require(len(codes) <= 100)
    for code, number in codes.items():
        _require(type(code) is str and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", code))
        _require(_integer(number, maximum=2 * limit + MAX_TRACKED_SIGNALS) > 0)

    merged = sorted(set(current_ids) | set(proof_ids))
    _require(len(merged) <= MAX_TRACKED_SIGNALS)
    result = deepcopy(current)
    target = result["queue_diagnostics"][lane]
    target["lag_signal_ids"] = merged
    target.pop(_GAP)
    target.pop(_SWEEPS, None)
    target["status"] = "UNKNOWN"
    target["reason"] = "LEGACY_COVERAGE_VERIFIED_AWAITING_MONITOR"
    target["legacy_coverage_proof"] = {
        "schema": "legacy-queue-coverage-proof-v1",
        "legacy_observed_at_ms": legacy_at,
        "legacy_count": count,
        "legacy_oldest_received_at_ms": oldest,
        "recent_from_ms": lower,
        "recent_to_ms": cutoff,
        "ready_keys": ready,
        "recent_keys": recent,
        "remaining_lag_count": lag_count,
        "retained_current_key_count": len(current_ids),
        "merged_key_count": len(merged),
        "limit_per_source": limit,
        "query_ms": elapsed,
        "recorded_at_ms": now,
        "alarm_clearance_authorized": False,
    }
    return result
