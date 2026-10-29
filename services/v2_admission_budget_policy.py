"""Inspect/CAS-update SANDBOX admission quota allocation, never trading risk.

This separate state namespace preserves older market-policy readers. Changing
the allocation does not increase the shared/noncritical host limits. Operators
must deploy compatible market and admission consumers before enabling a reserve.
"""

import argparse
import json
from uuid import uuid4

from services.v2_testnet_inventory import deployment_database
from v2_core.shared_market import AdmissionBudgetPolicy, MarketPolicy
from v2_core.state import BusinessState, StateKey


def main(argv=None, *, connect=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-json")
    parser.add_argument("--expected-version", type=int)
    args = parser.parse_args(argv)
    if args.set_json is not None and args.expected_version is None:
        parser.error("--expected-version is required for edits")
    connect = deployment_database() if connect is None else connect
    state = BusinessState(connect)
    key = StateKey(
        "BINANCE",
        "market-data",
        "SANDBOX",
        "FUTURES",
        "admission-budget-policy",
        "current",
    )
    market = MarketPolicy.read(connect, "SANDBOX")
    if args.set_json is not None:
        values = json.loads(args.set_json)
        if not isinstance(values, dict):
            raise ValueError("ADMISSION_POLICY_OBJECT_REQUIRED")
        policy = AdmissionBudgetPolicy(**values).validate_against(market)
        result = state.change(
            key,
            expected_version=args.expected_version,
            request_key=str(uuid4()),
            payload=policy.values(),
            reason="OPERATOR_ADMISSION_QUOTA_ALLOCATION",
        )
        if result.code != "APPLIED":
            raise ValueError("ADMISSION_POLICY_VERSION_CONFLICT")
    row = state.read(key)
    policy = AdmissionBudgetPolicy.read(connect, "SANDBOX").validate_against(market)
    result = {
        "environment": "SANDBOX",
        "version": row.version if row else 0,
        "policy": policy.values(),
        "host_total_limit": market.shared_weight_limit,
        "noncritical_total_limit": market.market_weight_limit,
        "market_lane_limit": market.market_weight_limit
        - policy.admission_reserved_weight,
        "admission_lane_limit": market.market_weight_limit
        - policy.market_reserved_weight,
        "management_reserved_weight": market.shared_weight_limit
        - market.market_weight_limit,
    }
    print(json.dumps(result, sort_keys=True))
    return result


if __name__ == "__main__":
    main()
