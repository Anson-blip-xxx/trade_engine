"""Inspect or CAS-update the versioned SANDBOX market policy; contains no secrets."""

import argparse
import json
from uuid import uuid4

from services.v2_testnet_inventory import deployment_database
from v2_core.shared_market import MarketPolicy
from v2_core.state import BusinessState, StateKey


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-json")
    parser.add_argument("--expected-version", type=int)
    args = parser.parse_args()
    connect = deployment_database()
    state = BusinessState(connect)
    key = StateKey(
        "BINANCE",
        "market-data",
        "SANDBOX",
        "FUTURES",
        "shared-market-policy",
        "current",
    )
    if args.set_json is not None:
        if args.expected_version is None:
            parser.error("--expected-version is required for edits")
        policy = MarketPolicy(**json.loads(args.set_json))
        result = state.change(
            key,
            expected_version=args.expected_version,
            request_key=str(uuid4()),
            payload=policy.values(),
            reason="OPERATOR_MARKET_POLICY_UPDATE",
        )
        if result.code != "APPLIED":
            raise ValueError("MARKET_POLICY_VERSION_CONFLICT")
    row = state.read(key)
    print(
        json.dumps(
            {
                "version": row.version if row else 0,
                "policy": MarketPolicy.read(connect, "SANDBOX").values(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
