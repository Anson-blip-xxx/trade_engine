"""Inspect/patch the isolated Testnet PG policy; never submits exchange orders.

Decimal settings are JSON strings. Every write requires the observed version
and an operator reason. No credentials or local state files are involved.
"""

import argparse
import json

from services.v2_testnet_inventory import deployment_database
from v2_core.account_risk import AccountScope
from v2_core.runtime_policy import SCHEMA, PolicyStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--account-id", required=True)
    parser.add_argument("action", choices=("schema", "show", "patch"))
    parser.add_argument("--changes", help="JSON object, exact decimals as strings")
    parser.add_argument("--expected-version", type=int)
    parser.add_argument("--reason")
    args = parser.parse_args()
    if args.action == "schema":
        print(
            json.dumps(
                {
                    name: {"default": spec[0], "minimum": spec[1], "maximum": spec[2]}
                    for name, spec in SCHEMA.items()
                },
                indent=2,
            )
        )
        return
    store = PolicyStore(
        deployment_database(),
        AccountScope("BINANCE", args.account_id, "SANDBOX", "FUTURES"),
    )
    if args.action == "patch":
        if args.expected_version is None or not args.reason or args.changes is None:
            parser.error("patch requires --expected-version, --reason and --changes")
        snapshot = store.patch(
            json.loads(args.changes),
            expected_version=args.expected_version,
            reason=args.reason,
        )
    else:
        snapshot = store.read()
    print(
        json.dumps(
            {
                "version": snapshot.version,
                "digest": snapshot.digest,
                "values": snapshot.values,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
