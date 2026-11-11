"""Bounded, isolated comparison driver; no database/network/order dependencies.

The caller owns read-only, scoped data collection. Bad candidates become explicit
unavailable rows and cannot interrupt another candidate or the trading consumer.
This module is intentionally not imported by any live decision hot path.
"""

from v2_core.trend_runner import (
    StagnationShadowPolicy,
    TrendRunnerPolicy,
    run_shadow,
    stagnation_shadow,
)


def compare_candidates(candidates, *, policy=None, max_candidates=100):
    if type(max_candidates) is not int or not 1 <= max_candidates <= 100:
        raise ValueError("TREND_RUNNER_BATCH_BOUND")
    if not isinstance(candidates, list) or len(candidates) > max_candidates:
        raise ValueError("TREND_RUNNER_BATCH_BOUND")
    policy = TrendRunnerPolicy() if policy is None else policy
    if not isinstance(policy, TrendRunnerPolicy):
        raise TypeError("TREND_RUNNER_POLICY_TYPE")
    reports = []
    for index, payload in enumerate(candidates):
        try:
            report = run_shadow(payload, policy=policy)
        except (ValueError, TypeError, KeyError, ArithmeticError):
            # No raw payload, exception or account data enters diagnostics.
            report = {
                "status": "UNAVAILABLE",
                "reason": "INVALID_CANDIDATE_CONTRACT",
                "execution_authorized": False,
                "profitability_valid": False,
                "actual_pnl": False,
            }
        reports.append({"candidate_index": index, "report": report})
    return {
        "mode": policy.mode,
        "candidate_count": len(candidates),
        "execution_authorized": False,
        "profitability_valid": False,
        "reports": reports,
    }


def compare_stagnation_candidates(candidates, *, policy=None, max_candidates=100):
    """Scoped frozen-exit input adapter; never reads or writes a business store.

    A research collector must project real frozen evidence into the core input
    contract. In particular, it must NOT manufacture historical receipt times
    or native protection coverage from today's venue state. Missing evidence is
    a useful UNAVAILABLE result, not permission to hold a position longer.
    """
    if type(max_candidates) is not int or not 1 <= max_candidates <= 100:
        raise ValueError("STAGNATION_SHADOW_BATCH_BOUND")
    if not isinstance(candidates, list) or len(candidates) > max_candidates:
        raise ValueError("STAGNATION_SHADOW_BATCH_BOUND")
    policy = StagnationShadowPolicy() if policy is None else policy
    if not isinstance(policy, StagnationShadowPolicy):
        raise TypeError("STAGNATION_SHADOW_POLICY_TYPE")
    reports = []
    for index, payload in enumerate(candidates):
        try:
            report = stagnation_shadow(payload, policy=policy)
        except (ValueError, TypeError, KeyError, ArithmeticError):
            report = {
                "status": "UNAVAILABLE",
                "reason": "INVALID_FROZEN_STAGNATION_CONTRACT",
                "execution_authorized": False,
                "promotion_authorized": False,
                "profitability_valid": False,
                "actual_pnl": False,
            }
        reports.append({"candidate_index": index, "report": report})
    return {
        "mode": policy.mode,
        "candidate_count": len(candidates),
        "execution_authorized": False,
        "promotion_authorized": False,
        "profitability_valid": False,
        "reports": reports,
    }
