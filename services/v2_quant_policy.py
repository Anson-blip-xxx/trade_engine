"""Inspect/CAS-update SANDBOX research configuration; never calls a venue.

This changes shared candidate production, not account trading authorization.
No credentials or local business-state files. Unknown fields fail closed.
"""

import argparse
import json
from uuid import uuid4

from services.v2_quant_research import QuantPolicy
from services.v2_testnet_inventory import deployment_database
from v2_core.state import BusinessState


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set-json")
    parser.add_argument("--expected-version", type=int)
    args = parser.parse_args()
    connect = deployment_database()
    if args.set_json is not None:
        if args.expected_version is None:
            parser.error("updates require --expected-version")
        result = QuantPolicy(**json.loads(args.set_json)).save(
            connect,
            "SANDBOX",
            expected_version=args.expected_version,
            request_key=str(uuid4()),
        )
        if result.code != "APPLIED":
            raise ValueError("QUANT_POLICY_VERSION_CONFLICT")
    row = BusinessState(connect).read(QuantPolicy.key("SANDBOX"))
    print(
        json.dumps(
            {
                "version": row.version if row else 0,
                "policy": QuantPolicy.read(connect, "SANDBOX").values(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
