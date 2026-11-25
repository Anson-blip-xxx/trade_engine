"""Frozen descriptive gate sensitivity; no I/O, tuning or trading authority.

The one registered volume alternative is 0.75 versus a frozen baseline of 1.
Full replay requires a complete supported frozen policy schema, signal and
bound market context; current defaults never fill a historical policy. Registered
historical schemas have explicit, reported pure-market compatibility adapters.
A metric-only projection can compare that one inequality, but cannot replay
the strategy.
Regime alternatives require separate research: this module never changes the
observed regime or manufactures relative-strength evidence.
"""

import re
from copy import deepcopy
from decimal import Decimal

from services.v2_trend_entry import VERSION as EVALUATOR_VERSION
from services.v2_trend_entry import evaluate_trend_market
from v2_core.evidence import canonical, digest
from v2_core.research_market_sources import frozen_source_review
from v2_core.runtime_policy import SCHEMA, resolve

VERSION = "mover-gate-shadow-v1"
POLICY_COMPATIBILITY_VERSION = "frozen-policy-compatibility-20261125-v2"
# Recorded from the 193-key deployed pre-entry-quality schema. Do not derive
# this allowlist from today's SCHEMA: another missing/added key must fail closed.
_PRE_ENTRY_QUALITY_SCHEMA_DIGEST = (
    "63e742ad4be439950afa8fc84faf5ca735cc66672cd4879e668c93dfbb2b8ef0"
)
_LEGACY_DISABLED_FEATURES = {
    "trend.breakout_body_guard_enabled": False,
    "trend.entry_quality_shadow_enabled": False,
}
# Recorded key-set identities, not a subset inferred from a mutable SCHEMA.
_PRE_CASH_SCHEMA_DIGEST = (
    "c973c0abf5c486112b6a3d83002903259e9d9eba5c60ae9ad3448cd38266ed75"
)
_CASH_SCHEMA_DIGEST = "1c2dd3ff0dd19ad1b7f59b1e0a1088c6a652bad22baa4d20b152dc05971dffcb"
_SOURCE_SCHEMA_DIGESTS = frozenset(
    {_PRE_ENTRY_QUALITY_SCHEMA_DIGEST, _PRE_CASH_SCHEMA_DIGEST, _CASH_SCHEMA_DIGEST}
)
# These fixed values encode the legacy (non-venue cash) behavior. They never
# activate accounting or enter an order path; this module evaluates market gates
# only. None of these fields is read by evaluate_trend_market.
_LEGACY_CASH_VALUES = {
    "settlement.cash_basis_version": 1,
    "settlement.max_difference_per_fill_usdt": "0.000001",
    "settlement.max_difference_episode_usdt": "0.0001",
}
MAX_SAMPLES = 100
_VOLUME_KEY = "trend.min_volume_ratio"
_BASELINE = "1"
_ALTERNATIVE = "0.75"
_REGIMES = frozenset(
    {
        "bull_trend",
        "range",
        "strong_bull",
        "weak_bull",
        "neutral",
        "weak_bear",
        "risk-off",
        "risk_off",
    }
)
_MISSING_MARKET = frozenset(
    {"TREND_STRUCTURE_UNCONFIRMED", "TREND_STRUCTURE_STALE", "TREND_WARMUP"}
)
_REGIME_REASON = "REQUIRES_RELATIVE_STRENGTH_AND_LIVE_VALIDATION"
_HOST_ENVIRONMENTS = {
    "fapi.binance.com": "LIVE",
    "demo-fapi.binance.com": "SANDBOX",
}
_SOURCE_ENVIRONMENTS = {
    "BINANCE_USDM_LIVE": "LIVE",
    "BINANCE_USDM_TESTNET": "SANDBOX",
    "BINANCE_USDM_SANDBOX": "SANDBOX",
}


class _MissingEvidence(ValueError):
    """Only fixed internal codes are allowed in missing-evidence output."""


def _decimal(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 80:
        raise ValueError("INVALID_FROZEN_NUMBER")
    parsed = Decimal(value)
    if not parsed.is_finite() or parsed < 0:
        raise ValueError("INVALID_FROZEN_NUMBER")
    return parsed


def _unknown_volume(reason):
    return {
        "status": "UNKNOWN",
        "reason": reason,
        "baseline_gate": "UNKNOWN",
        "candidate_gate": "UNKNOWN",
        "gate_changed": None,
        "volume_ratio": None,
        "baseline_market_reason": None,
        "candidate_market_reason": None,
        "other_gate_status": "UNKNOWN",
    }


def _volume_comparison(value):
    if value is None:
        return _unknown_volume("MISSING_VOLUME_RATIO")
    ratio = _decimal(value)
    baseline = ratio >= Decimal(_BASELINE)
    candidate = ratio >= Decimal(_ALTERNATIVE)
    return {
        **_unknown_volume("SINGLE_GATE_COMPARISON_ONLY"),
        "status": "COMPARED",
        "baseline_gate": "PASS" if baseline else "FAIL",
        "candidate_gate": "PASS" if candidate else "FAIL",
        "gate_changed": baseline != candidate,
        "volume_ratio": value,
    }


def _market_source_consistency(sources, environment):
    """Reject known contradictory provenance, never authenticate supplied labels.

    Only exact, known host/provider names carry an environment here. Generic
    BINANCE_FUTURES and unknown names do not prove LIVE or SANDBOX. Liquidity
    provenance is a separate input and must not be confused with volume bars.
    """
    if not isinstance(sources, dict):
        return
    for fields, known in (
        (("market_host", "host", "source_host"), _HOST_ENVIRONMENTS),
        (
            ("market_source", "structure_source", "s3_source", "source"),
            _SOURCE_ENVIRONMENTS,
        ),
    ):
        for key in fields:
            value = sources.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise TypeError("INVALID_FROZEN_MARKET_SOURCE")
            inferred = known.get(value)
            if inferred is not None and inferred != environment:
                raise ValueError("FROZEN_MARKET_SOURCE_ENVIRONMENT_MISMATCH")


def _environment(sample, environment):
    sources = sample.get("market_sources")
    _market_source_consistency(sources, environment)
    if sample.get("require_market_environment") is True:
        if not isinstance(sources, dict):
            raise _MissingEvidence("MISSING_FROZEN_MARKET_ENVIRONMENT")
        observed_sources = [
            sources[key]
            for key in ("market_environment", "structure_environment", "s3_environment")
            if sources.get(key) is not None
        ]
        if not observed_sources:
            raise _MissingEvidence("MISSING_FROZEN_MARKET_ENVIRONMENT")
        if any(value != environment for value in observed_sources):
            raise ValueError("FROZEN_MARKET_ENVIRONMENT_MISMATCH")
    context = sample.get("context")
    if isinstance(context, dict):
        directional = context.get("directional")
        market = directional.get("market") if isinstance(directional, dict) else None
        if isinstance(market, dict):
            _market_source_consistency(market.get("input_evidence"), environment)
    observed = context.get("environment") if isinstance(context, dict) else None
    explicit = sample.get("environment")
    if observed is None and explicit is None:
        raise _MissingEvidence("MISSING_MARKET_ENVIRONMENT")
    if any(value != environment for value in (observed, explicit) if value is not None):
        raise ValueError("ENVIRONMENT_MISMATCH")
    for scope in (
        sample.get("config", {}).get("account_scope"),
        context.get("account_scope") if isinstance(context, dict) else None,
    ):
        if scope is not None and (
            not isinstance(scope, dict) or scope.get("environment") != environment
        ):
            raise ValueError("ENVIRONMENT_MISMATCH")


def _validate_supplied_policy(policy):
    """Validate the explicit cash contract and reject unknown settings.

    Partial cash contracts cannot be repaired using defaults, including in a
    metric-only comparison. The research evaluator must use the 198-key package.
    """
    supplied_cash = set(policy) & _LEGACY_CASH_VALUES.keys()
    if supplied_cash and supplied_cash != _LEGACY_CASH_VALUES.keys():
        raise _MissingEvidence("INCOMPLETE_FROZEN_POLICY")
    if set(policy) - SCHEMA.keys():
        raise ValueError("UNKNOWN_FROZEN_POLICY_SETTING")
    for key in supplied_cash:
        value = policy[key]
        if key == "settlement.cash_basis_version":
            if type(value) is not int or value not in {1, 2}:
                raise ValueError("INVALID_FROZEN_CASH_CONTRACT")
        elif type(value) is not str or _decimal(value) > Decimal("0.01"):
            raise ValueError("INVALID_FROZEN_CASH_CONTRACT")
    # This validates supplied ordinary values. Any defaults in this intermediate
    # result are discarded; a full replay separately requires a registered,
    # complete source schema and an explicitly completed evaluation policy.
    resolve(policy)


def _replay_config(config):
    """Adapt registered complete schemas without changing original evidence.

    This is pure market-gate replay, NOT accounting replay or historical code
    equivalence. Every added value and both hashes remain visible.
    """
    policy = config["policy"]
    _validate_supplied_policy(policy)
    keys = set(policy)
    schema_digest = digest(canonical({"keys": sorted(keys)}))
    evaluator_digest = digest(canonical({"keys": sorted(SCHEMA)}))
    if (
        schema_digest not in _SOURCE_SCHEMA_DIGESTS
        or evaluator_digest != _CASH_SCHEMA_DIGEST
    ):
        raise _MissingEvidence("INCOMPLETE_FROZEN_POLICY")
    additions = {}
    migration = None
    if schema_digest == _PRE_ENTRY_QUALITY_SCHEMA_DIGEST:
        additions.update(_LEGACY_DISABLED_FEATURES)
        migration = "PRE_ENTRY_QUALITY_20261107_TO_DISABLED_V1"
    cash_adaptation = None
    if not (keys & _LEGACY_CASH_VALUES.keys()):
        additions.update(_LEGACY_CASH_VALUES)
        cash_adaptation = "EXPLICIT_LEGACY_CASH_BASIS_V1"
    adapted = deepcopy(config)
    adapted["policy"].update(additions)
    if set(adapted["policy"]) != set(SCHEMA):
        raise _MissingEvidence("INCOMPLETE_FROZEN_POLICY")
    resolve(adapted["policy"])
    mode = "EXPLICIT_LEGACY_SCHEMA_ADAPTER" if additions else "EXACT_FROZEN_POLICY"
    return adapted, {
        "version": POLICY_COMPATIBILITY_VERSION,
        "mode": mode,
        "migration": migration,
        "source_schema_digest": schema_digest,
        "evaluator_schema_digest": evaluator_digest,
        "source_config_digest": digest(canonical(config)),
        "evaluation_config_digest": digest(canonical(adapted)),
        "explicit_legacy_values": additions,
        "cash_basis_adaptation": cash_adaptation,
        "adaptation_scope": "PURE_MARKET_GATES_ONLY_NOT_ACCOUNTING_REPLAY",
        "original_evidence_modified": False,
        "historical_code_identity": "NOT_PROVEN",
    }


def _validate_replay(sample):
    """Require evidence used by this evaluator, without inventing account data."""
    signal, context, config = (
        sample.get(key) for key in ("signal", "context", "config")
    )
    if not isinstance(signal, dict) or not isinstance(context, dict):
        raise _MissingEvidence("MISSING_FROZEN_SIGNAL_OR_CONTEXT")
    config, compatibility = _replay_config(config)
    policy = resolve(config["policy"])
    if config.get("source") not in {"s3", "tv_bridge"} or config.get(
        "strategy"
    ) not in {"S6", "S8"}:
        raise _MissingEvidence("MISSING_SUPPORTED_SOURCE_OR_STRATEGY")
    if policy["entry.trend_structure_enabled"] is not True:
        raise _MissingEvidence("FROZEN_TREND_PROFILE_DISABLED")
    directional = context.get("directional")
    if not isinstance(directional, dict):
        raise _MissingEvidence("MISSING_FROZEN_DIRECTIONAL_CONTEXT")
    required_signal = {"symbol", "signal", "observed_at", "expires_at_ms", "features"}
    if (
        not required_signal <= signal.keys()
        or not {"symbol", "assembled_at", "sources"} <= context.keys()
    ):
        raise _MissingEvidence("INCOMPLETE_FROZEN_SIGNAL_OR_CONTEXT")
    if not {"market", "price", "regime"} <= directional.keys():
        raise _MissingEvidence("INCOMPLETE_FROZEN_DIRECTIONAL_CONTEXT")
    if not isinstance(directional["market"], dict) or not isinstance(
        context["sources"], dict
    ):
        raise TypeError("INVALID_FROZEN_MARKET_CONTEXT")
    if directional["regime"] not in _REGIMES:
        raise _MissingEvidence("UNKNOWN_OBSERVED_REGIME")
    if (
        signal["symbol"] != context["symbol"]
        or sample.get("symbol", signal["symbol"]) != signal["symbol"]
    ):
        raise ValueError("SYMBOL_MISMATCH")
    observed, expires, assembled = (
        signal["observed_at"],
        signal["expires_at_ms"],
        context["assembled_at"],
    )
    if (
        any(type(value) is not int for value in (observed, expires, assembled))
        or not 0 <= observed <= assembled < expires < 2**53
    ):
        raise ValueError("INVALID_FROZEN_TIMESTAMPS")
    if not isinstance(signal["features"], dict) or "strength" not in signal["features"]:
        raise _MissingEvidence("MISSING_FROZEN_SIGNAL_STRENGTH")
    if _decimal(directional["price"]) <= 0:
        raise ValueError("INVALID_FROZEN_PRICE")
    return config, compatibility


def _metrics(sample):
    metrics = sample.get("frozen_metrics")
    if not isinstance(metrics, dict) or "volume_ratio" not in metrics:
        raise _MissingEvidence("MISSING_FROZEN_VOLUME_METRIC")
    if set(metrics) - {"volume_ratio", "taker_buy_ratio"}:
        raise ValueError("UNKNOWN_FROZEN_METRIC")
    flow = metrics.get("taker_buy_ratio")
    if flow is not None and _decimal(flow) > 1:
        raise ValueError("INVALID_FROZEN_FLOW")
    return _volume_comparison(metrics["volume_ratio"])


def _evaluate_sample(sample, index, environment):
    result = {
        "index": index,
        "evidence_ref": None,
        "mode": "UNKNOWN",
        "error": False,
        "degraded": True,
        "input_digest": None,
        "config_digest": None,
        "policy_compatibility": None,
        "market_provenance": None,
        "volume": _unknown_volume("MISSING_FROZEN_INPUT"),
        "regime": {
            "status": "UNKNOWN",
            "reason": _REGIME_REASON,
            "observed_regime": None,
        },
    }
    try:
        if not isinstance(sample, dict):
            raise TypeError("INVALID_FROZEN_SAMPLE")
        encoded = canonical(sample)
        if len(encoded) > 512000:
            raise ValueError("FROZEN_SAMPLE_TOO_LARGE")
        result["input_digest"] = digest(encoded)
        reference = sample.get("evidence_ref")
        if reference is not None:
            if not isinstance(reference, str) or not re.fullmatch(
                r"[0-9a-f]{64}", reference
            ):
                raise ValueError("INVALID_EVIDENCE_REFERENCE")
            result["evidence_ref"] = reference
        config = sample.get("config")
        if not isinstance(config, dict) or not isinstance(config.get("policy"), dict):
            raise _MissingEvidence("MISSING_FROZEN_CONFIG")
        result["config_digest"] = digest(canonical(config))
        if (
            sample.get("config_digest", result["config_digest"])
            != result["config_digest"]
        ):
            raise ValueError("FROZEN_CONFIG_DIGEST_MISMATCH")
        _environment(sample, environment)
        result["market_provenance"] = frozen_source_review(
            sample, environment=environment
        )
        if result["market_provenance"]["replay_blocked"]:
            source_reason = result["market_provenance"]["reason"]
            # Preserve the established setup-study rejection category; the
            # source projection retains the more specific binding diagnostic.
            raise _MissingEvidence(
                "TREND_STRUCTURE_UNCONFIRMED"
                if source_reason == "FROZEN_SOURCE_BINDING_MISMATCH"
                else source_reason
            )
        threshold = config["policy"].get(_VOLUME_KEY)
        if threshold is None:
            raise _MissingEvidence("MISSING_FROZEN_VOLUME_THRESHOLD")
        if _decimal(threshold) != Decimal(_BASELINE):
            raise _MissingEvidence("BASELINE_OUTSIDE_FROZEN_STUDY_PLAN")
        _validate_supplied_policy(config["policy"])
        try:
            replay_config, compatibility = _validate_replay(sample)
        except _MissingEvidence as incomplete:
            result["replay_unavailable_reason"] = str(incomplete)
            if "frozen_metrics" not in sample:
                raise
            result["volume"] = _metrics(sample)
            result["mode"] = "FROZEN_METRIC_ONLY"
            return result

        result["policy_compatibility"] = compatibility
        signal, context = sample["signal"], sample["context"]
        result["regime"]["observed_regime"] = context["directional"]["regime"]
        baseline, proof = evaluate_trend_market(
            deepcopy(signal), deepcopy(context), deepcopy(replay_config)
        )
        if baseline.reason in _MISSING_MARKET:
            raise _MissingEvidence(baseline.reason)
        structure = proof.get("structure")
        if not isinstance(structure, dict) or structure.get("status") != "READY":
            raise _MissingEvidence("MISSING_VALIDATED_STRUCTURE")
        values = structure["values"]
        if "frozen_metrics" in sample:
            _metrics(sample)
            for key, value in sample["frozen_metrics"].items():
                if value != values.get(key):
                    raise ValueError("FROZEN_METRIC_CONTEXT_MISMATCH")
        volume = _volume_comparison(values["volume_ratio"])
        alternate_config = deepcopy(replay_config)
        alternate_config["policy"][_VOLUME_KEY] = _ALTERNATIVE
        alternative, _ = evaluate_trend_market(
            deepcopy(signal), deepcopy(context), alternate_config
        )
        other_failure = alternative.reason not in {
            "CANDIDATE",
            "TREND_VOLUME_UNCONFIRMED",
        }
        volume.update(
            baseline_market_reason=baseline.reason,
            candidate_market_reason=alternative.reason,
            other_gate_status="STILL_FAILS"
            if other_failure
            else "NO_RECORDED_MARKET_FAILURE"
            if alternative.reason == "CANDIDATE"
            else "UNKNOWN",
        )
        # A valid zero-volume observation has unknown taker flow. Preserve the
        # actual rejection without calling missing flow a measured failed gate.
        if (
            alternative.reason == "TREND_FLOW_UNCONFIRMED"
            and values["taker_buy_ratio"] is None
        ):
            volume["other_gate_status"] = "UNKNOWN"
        result.update(
            mode="FULL_FROZEN_REPLAY",
            degraded=volume["status"] == "UNKNOWN",
            volume=volume,
        )
    except _MissingEvidence as missing:
        result["volume"] = _unknown_volume(str(missing))
    except (
        TypeError,
        ValueError,
        KeyError,
        AttributeError,
        ArithmeticError,
        RecursionError,
    ):
        # No exception text, raw config, account data or evidence blob escapes.
        result.update(error=True, volume=_unknown_volume("INVALID_FROZEN_INPUT"))
    return result


def evaluate_mover_gate_shadow(samples, *, environment):
    """Return a bounded JSON report, isolating missing/invalid individual rows.

    Each row accepts ``signal/context/config`` from frozen evidence, optional
    ``evidence_ref/config_digest``, and an explicit ``environment`` for a
    metric-only row. ``frozen_metrics`` may contain decimal-string
    ``volume_ratio`` and ``taker_buy_ratio``; it never supplies missing replay
    context. Full replay requires every key of a supported historical schema;
    any registered legacy adaptation is explicitly reported with separate hashes.

    The caller retains the input artifacts. Hashes here support repeatability,
    not authenticity; no historical source/code or publication latency is
    independently verified. Results are market-gate observations only, never
    order, admission, fill, profit, or deployment predictions.
    """
    if environment not in {"SANDBOX", "LIVE"}:
        raise ValueError("EXPLICIT_SHADOW_ENVIRONMENT_REQUIRED")
    if not isinstance(samples, (list, tuple)) or len(samples) > MAX_SAMPLES:
        raise ValueError("BOUNDED_SHADOW_SAMPLES_REQUIRED")
    rows = [
        _evaluate_sample(sample, index, environment)
        for index, sample in enumerate(samples)
    ]
    summary = {
        "sample_count": len(rows),
        "replayed_count": sum(row["mode"] == "FULL_FROZEN_REPLAY" for row in rows),
        "metric_only_count": sum(row["mode"] == "FROZEN_METRIC_ONLY" for row in rows),
        "volume_gate_changed_count": sum(
            row["volume"]["gate_changed"] is True for row in rows
        ),
        "other_gate_still_failed_count": sum(
            row["volume"]["other_gate_status"] == "STILL_FAILS" for row in rows
        ),
        "unknown_count": sum(row["volume"]["status"] == "UNKNOWN" for row in rows),
        "degraded_count": sum(row["degraded"] for row in rows),
        "error_count": sum(row["error"] for row in rows),
        "regime_unknown_count": len(rows),
    }
    return {
        "version": VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "environment": environment,
        "mode": "RESEARCH_ONLY",
        "execution_authorized": False,
        "not_trade_prediction": True,
        "automatic_parameter_selection": False,
        "market_data_scope": "SANDBOX_NOT_LIVE"
        if environment == "SANDBOX"
        else "LIVE_OBSERVATIONS_ONLY",
        "market_source_provenance": "NOT_VERIFIED_BY_SHADOW",
        "market_source_notice": "ENVIRONMENT_LABEL_IS_NOT_MARKET_SOURCE_AUTHENTICATION",
        "source_checks": {
            "fresh_at_evaluation_count": sum(
                isinstance(row["market_provenance"], dict)
                and row["market_provenance"]["recorded_freshness_confirmed"]
                for row in rows
            ),
            "blocked_count": sum(
                isinstance(row["market_provenance"], dict)
                and row["market_provenance"]["replay_blocked"]
                for row in rows
            ),
            "source_authenticity": "NOT_INDEPENDENTLY_VERIFIED",
        },
        "out_of_sample_validation": "NOT_COMPLETED",
        "cohort_scope": "SELECTED_MOVER_DIAGNOSTIC_NOT_OUT_OF_SAMPLE",
        "input_authenticity": "CALLER_FROZEN_EVIDENCE_NOT_INDEPENDENTLY_VERIFIED",
        "volume_plan": {
            "baseline_min_volume_ratio": _BASELINE,
            "candidate_min_volume_ratio": _ALTERNATIVE,
        },
        "regime_plan": {"status": "UNKNOWN", "reason": _REGIME_REASON},
        "summary": summary,
        "samples": rows,
    }
