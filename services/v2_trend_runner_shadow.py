"""Bounded, isolated comparison driver; no database/network/order dependencies.

The caller owns read-only, scoped data collection. Bad candidates become explicit
unavailable rows and cannot interrupt another candidate or the trading consumer.
This module is intentionally not imported by any live decision hot path.
"""

from v2_core.trend_runner import TrendRunnerPolicy, run_shadow


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
