"""Read-only schema compatibility check; never imports deployed service code.

Usage: python -m scripts.v2_entry_release_preflight --candidate PATH
       --reader EXECUTION_RELEASE --reader ACCOUNT_ADMIN_RELEASE [...]
Every policy reader/writer must be listed by the release manifest. Passing does
not check DB grants, migrations or authorize deployment/trading.
"""

import argparse
import ast
import json
from pathlib import Path


def schema(root):
    tree = ast.parse((Path(root) / "v2_core/runtime_policy.py").read_text())
    values = [
        n.value
        for n in tree.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "SCHEMA" for t in n.targets)
    ]
    if len(values) != 1 or not isinstance(values[0], ast.Dict):
        raise ValueError("POLICY_SCHEMA_NOT_LITERAL")
    value = ast.literal_eval(values[0])
    if (
        not isinstance(value, dict)
        or not value
        or any(
            not isinstance(k, str) or not isinstance(v, tuple) or len(v) != 3
            for k, v in value.items()
        )
    ):
        raise ValueError("POLICY_SCHEMA_INVALID")
    return value


def inspect(candidate, readers):
    required = schema(candidate)
    if not readers:
        raise ValueError("EXPLICIT_POLICY_READERS_REQUIRED")
    results = []
    for path in readers:
        try:
            current = schema(path)
            missing = sorted(set(required) - set(current))
            extra = sorted(set(current) - set(required))
            changed = sorted(
                k
                for k in required.keys() & current.keys()
                if required[k] != current[k]
                or tuple(map(type, required[k])) != tuple(map(type, current[k]))
            )
            results.append(
                {
                    "reader": str(Path(path).resolve()),
                    "compatible": not (missing or extra or changed),
                    "missing": missing,
                    "extra": extra,
                    "changed": changed,
                }
            )
        except (OSError, SyntaxError, ValueError):
            results.append(
                {
                    "reader": str(path),
                    "compatible": False,
                    "reason": "UNREADABLE_OR_INVALID_SCHEMA",
                }
            )
    return {
        "status": "COMPATIBLE" if all(r["compatible"] for r in results) else "BLOCKED",
        "readers": results,
        "execution_authorized": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--reader", required=True, action="append")
    args = parser.parse_args()
    result = inspect(args.candidate, args.reader)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "COMPATIBLE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
