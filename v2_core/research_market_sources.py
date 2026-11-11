"""Pure provenance checks for research; no market, account or execution ports.

Source labels and hashes describe frozen inputs, not independent authenticity.
Unknown freshness remains unknown; explicit expiry cannot confirm a candidate.
"""

from v2_core.evidence import canonical, digest
from v2_core.signal_fusion import validate_fusion

VERSION = "research-market-sources-v1"
_ENVIRONMENTS = {"LIVE", "SANDBOX"}
_SOURCES = {
    "BINANCE_USDM_LIVE": "LIVE",
    "BINANCE_USDM_SANDBOX": "SANDBOX",
    "BINANCE_USDM_TESTNET": "SANDBOX",
}
_HOSTS = {"fapi.binance.com": "LIVE", "demo-fapi.binance.com": "SANDBOX"}


def _clock(value):
    return type(value) is int and 0 <= value < 2**53


def _environment(value):
    return isinstance(value, str) and value in _ENVIRONMENTS


def market_comparison_contract(*, research_source, execution_environment):
    """Environment equality is necessary, never sufficient, for PnL attribution."""
    research = (
        _SOURCES.get(research_source) if isinstance(research_source, str) else None
    )
    execution = execution_environment if _environment(execution_environment) else None
    match = research == execution if research and execution else None
    return {
        "version": VERSION,
        "research_environment": research or "UNKNOWN",
        "execution_environment": execution or "UNKNOWN",
        "environment_match": match,
        "comparison_scope": "CROSS_ENVIRONMENT_RESEARCH_ONLY"
        if match is False
        else "SAME_ENVIRONMENT_REQUIRES_BOUND_PRICE_PATH"
        if match is True
        else "SOURCE_UNVERIFIED",
        "cross_environment_pnl_permitted": False,
        "executable_profit_proven": False,
        "execution_authorized": False,
        "catalog_absence_is_strategy_rejection": False,
        "historical_listing_proven_by_current_catalog": False,
        "source_authenticity": "NOT_INDEPENDENTLY_VERIFIED",
        "paired_observations": "NOT_ESTABLISHED_BY_ENVIRONMENT_LABELS",
    }


def frozen_source_review(sample, *, environment):
    """Project bounded source/time facts at the frozen evaluation time.

    Legacy inputs without a recorded freshness policy can still support descriptive
    arithmetic. They cannot establish timely source confirmation. No current clock,
    current policy or default freshness window is substituted for missing evidence.
    """
    result = {
        "version": VERSION,
        "environment": environment if _environment(environment) else "UNKNOWN",
        "status": "UNKNOWN",
        "reason": "FROZEN_SOURCE_METADATA_MISSING",
        "freshness": "UNKNOWN",
        "recorded_freshness_confirmed": False,
        "replay_blocked": False,
        "source_host": None,
        "source_authenticity": "NOT_INDEPENDENTLY_VERIFIED",
        "source_snapshot_digest_checked": False,
        "historical_publication_latency_verified": False,
        "evaluated_at_ms": None,
        "market_observed_at_ms": None,
        "context_valid_until_ms": None,
        "tv_confirmation": "UNKNOWN",
        "tv_market_environment": "UNKNOWN",
        "tv_environment_notice": "INGRESS_ENVIRONMENT_DOES_NOT_IDENTIFY_CHART_MARKET",
        "execution_authorized": False,
    }

    def blocked(reason):
        return {
            **result,
            "status": "UNAVAILABLE",
            "reason": reason,
            "replay_blocked": True,
            "recorded_freshness_confirmed": False,
        }

    if not isinstance(sample, dict) or not _environment(environment):
        return blocked("FROZEN_SOURCE_INPUT_INVALID")
    context, signal = sample.get("context"), sample.get("signal")
    if not isinstance(context, dict) or not isinstance(signal, dict):
        return result
    now = context.get("assembled_at")
    if not _clock(now):
        return blocked("FROZEN_SOURCE_TIME_INVALID")
    result["evaluated_at_ms"] = now
    observed, expires = signal.get("observed_at"), signal.get("expires_at_ms")
    if not _clock(observed) or not _clock(expires) or not observed <= now < expires:
        return blocked("FROZEN_SIGNAL_EXPIRED_OR_FUTURE")
    deadline = context.get("valid_until_ms")
    if deadline is not None:
        if not _clock(deadline) or now >= deadline:
            return blocked("FROZEN_CONTEXT_EXPIRED")
        result["context_valid_until_ms"] = deadline
    directional = context.get("directional")
    market = directional.get("market") if isinstance(directional, dict) else None
    meta = market.get("input_evidence") if isinstance(market, dict) else None
    sources = context.get("sources")
    s3 = sources.get("s3") if isinstance(sources, dict) else None
    if not isinstance(meta, dict) or not isinstance(s3, dict):
        return result
    if context.get("environment") != environment or any(
        item.get("environment") != environment for item in (meta, s3)
    ):
        return blocked("FROZEN_SOURCE_ENVIRONMENT_MISMATCH")
    host = meta.get("source_host")
    known_host = _HOSTS.get(host) if isinstance(host, str) else None
    known_source = (
        _SOURCES.get(meta.get("source"))
        if isinstance(meta.get("source"), str)
        else None
    )
    if any(value != environment for value in (known_host, known_source) if value):
        return blocked("FROZEN_SOURCE_ENVIRONMENT_MISMATCH")
    result["source_host"] = host if known_host else None
    closed = meta.get("closed_at")
    if not _clock(closed) or closed > now:
        return blocked("FROZEN_SOURCE_TIME_INVALID")
    result["market_observed_at_ms"] = closed
    if (
        s3.get("source") != "s3"
        or s3.get("symbol") != context.get("symbol")
        or not _clock(s3.get("observed_at"))
        or s3.get("observed_at") != closed
        or s3.get("features") != market
    ):
        return blocked("FROZEN_SOURCE_BINDING_MISMATCH")
    if s3.get("snapshot_id") != digest(
        canonical({key: value for key, value in s3.items() if key != "snapshot_id"})
    ):
        return blocked("FROZEN_SOURCE_DIGEST_MISMATCH")
    result["source_snapshot_digest_checked"] = True
    freshness = context.get("freshness_policy")
    if freshness is not None:
        if not isinstance(freshness, dict) or not freshness or len(freshness) > 3:
            return blocked("FROZEN_SOURCE_FRESHNESS_INVALID")
        for name, rule in freshness.items():
            item = sources.get(name)
            if (
                name not in {"s0", "s2", "s3"}
                or not isinstance(rule, dict)
                or not isinstance(item, dict)
            ):
                return blocked("FROZEN_SOURCE_FRESHNESS_INVALID")
            age, stamp = rule.get("max_age_ms"), item.get("observed_at")
            if type(age) is not int or not 0 <= age < 2**53 or not _clock(stamp):
                return blocked("FROZEN_SOURCE_FRESHNESS_INVALID")
            if item.get("environment") != environment or item.get("source") != name:
                return blocked("FROZEN_SOURCE_ENVIRONMENT_MISMATCH")
            if not 0 <= now - stamp <= age:
                result["freshness"] = "STALE_OR_FUTURE"
                return blocked("FROZEN_SOURCE_EXPIRED_OR_FUTURE")
        if "s3" in freshness and set(sources) <= set(freshness):
            result["freshness"] = "WITHIN_FROZEN_SOURCE_LIMITS"
    elif deadline is not None:
        result["freshness"] = "WITHIN_FROZEN_CONTEXT_DEADLINE"
    config = sample.get("config")
    fusion = context.get("signal_fusion")
    if fusion is not None:
        try:
            checked = validate_fusion(
                signal,
                source=config.get("source") if isinstance(config, dict) else None,
                proof=fusion,
                now_ms=now,
            )
        except (TypeError, ValueError, KeyError):
            return blocked("FROZEN_TV_PROOF_INVALID")
        if checked is not None:
            if checked.get("environment") != environment:
                return blocked("FROZEN_TV_ENVIRONMENT_MISMATCH")
            if checked["status"] in {"CONFIRMS", "CONFLICTS"}:
                result["tv_confirmation"] = checked["status"]
    result.update(
        status="CONSISTENT",
        reason="FROZEN_SOURCE_BINDINGS_CHECKED",
        recorded_freshness_confirmed=result["freshness"] != "UNKNOWN",
    )
    return result
