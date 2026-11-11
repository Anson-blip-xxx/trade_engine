"""Read-only staged dependency closure and no-I/O controller construction smoke.

Candidate modules take precedence. The reference tree is inspected only to
classify missing local modules and follow their transitive dependencies; it is
never placed on the callable smoke's import path. This is not a trading test.
"""

import argparse
import ast
import json
import os
import re
import subprocess
import sys
from collections import deque
from pathlib import Path

MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*\Z")

CONTROLLER_SMOKE = r"""
import json
import sys

imports_complete = False

def no_io(*args, **kwargs):
    raise AssertionError("PROBE_IO_FORBIDDEN")

def audit(event, args):
    if event in {"socket.connect", "socket.getaddrinfo"} or (
        imports_complete and event in {"subprocess.Popen", "os.system"}
    ):
        no_io()

sys.addaudithook(audit)
try:
    from services.v2_directional_admission import create_directional_admission_scheduler
    from services.v2_directional_replay import source_policy
    from v2_core.account_risk import AccountScope
    from v2_core.directional import directional_events
    from v2_core.runtime import DataRuntime
    # ctypes may invoke ldconfig to locate libpq during imports. No subprocess
    # is permitted while constructing or exercising the actual dry ports.
    imports_complete = True

    runtime = DataRuntime(
        no_io, submit=no_io, query=no_io, risk_check=no_io,
        clock_ms=lambda: 2, risk_reference=no_io,
        scope=AccountScope("BINANCE", "release-probe-synthetic", "SANDBOX", "FUTURES"),
    )
    counts, schedulers = {}, 0
    for strategy in ("S6", "S8"):
        events = directional_events(strategy)
        assert events and all(isinstance(x, str) for x in events)
        counts[strategy] = len(events)
        # Preserve the deployed admission surface: QUANT is not activated by
        # this release, even though directional_events imports its registry.
        for source in ("s3", "tv_bridge"):
            scheduler = create_directional_admission_scheduler(
                runtime, context_provider=no_io, strategy=strategy,
                analysis_mode="hard", max_delay_ms=30000,
                enable_admission=True, source=source, dynamic_universe=True,
                tv_start_ms=1,
            )
            assert scheduler.priority_events == events
            assert scheduler.actionable_events == events
            schedulers += 1
    signal = {"signal": "TREND_DOWN", "features": {"strength": 60}}
    for enabled, expected in (
        (False, "directional-default-v1"), (True, "causal-trend-entry-v1")
    ):
        policy, proof = source_policy(signal, {
            "source": "s3", "policy": {"entry.trend_structure_enabled": enabled}
        })
        assert proof["profile"] == expected
        assert policy["entry.trend_structure_enabled"] is enabled
    print(json.dumps({"passed": True, "event_counts": counts,
        "scheduler_constructions": schedulers, "source_profiles": 2,
        "database_calls": 0, "exchange_calls": 0}))
except Exception as exc:
    print(json.dumps({"passed": False, "error_type": type(exc).__name__}))
    raise SystemExit(1)
"""


def module_source(root, module):
    if not MODULE.fullmatch(module):
        return None
    path = root.joinpath(*module.split("."))
    for selected in (path.with_suffix(".py"), path / "__init__.py"):
        if selected.is_file():
            return selected
    return None


def inspect_closure(candidate, reference, entries):
    candidate, reference = Path(candidate).resolve(), Path(reference).resolve()
    if not candidate.is_dir() or not reference.is_dir():
        raise ValueError("PACKAGE_DIRECTORIES_REQUIRED")
    if not entries or any(not MODULE.fullmatch(item) for item in entries):
        raise ValueError("EXPLICIT_MODULE_ENTRIES_REQUIRED")
    queue, seen, missing, invalid = deque(entries), set(), set(), set()
    while queue:
        module = queue.popleft()
        if module in seen:
            continue
        seen.add(module)
        deployed = module_source(candidate, module)
        source = deployed or module_source(reference, module)
        if deployed is None:
            missing.add(module)
        if source is None:
            continue
        try:
            tree = ast.parse(source.read_text())
        except (OSError, UnicodeError, SyntaxError):
            invalid.add(module)
            continue
        package = module.split(".")
        if source.name != "__init__.py":
            package.pop()
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    if node.level > len(package):
                        invalid.add(module)
                        continue
                    base = ".".join(
                        package[: len(package) - node.level + 1]
                        + ([base] if base else [])
                    )
                names = [base, *(base + "." + item.name for item in node.names)]
            for name in names:
                if module_source(candidate, name) or module_source(reference, name):
                    queue.append(name)
    return {
        "passed": not missing and not invalid,
        "modules_inspected": len(seen),
        "missing_local_modules": sorted(missing),
        "invalid_local_modules": sorted(invalid),
        "dynamic_imports_completely_verified": False,
    }


def controller_smoke(candidate, *, python=sys.executable):
    candidate = Path(candidate).resolve()
    if not candidate.is_dir():
        raise ValueError("PACKAGE_DIRECTORY_REQUIRED")
    try:
        result = subprocess.run(
            [str(python), "-B", "-c", CONTROLLER_SMOKE],
            cwd=candidate,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "PYTHONPATH": str(candidate),
                "PYTHONDONTWRITEBYTECODE": "1",
            },
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        data = json.loads(result.stdout)
        if not isinstance(data, dict) or type(data.get("passed")) is not bool:
            raise ValueError("INVALID_SMOKE_RESPONSE")
        if result.returncode or not data["passed"]:
            kind = data.get("error_type")
            return {
                "passed": False,
                "error_type": kind
                if isinstance(kind, str) and re.fullmatch(r"[A-Za-z]+Error", kind)
                else "SMOKE_FAILED",
            }
        if data != {
            "passed": True,
            "event_counts": data.get("event_counts"),
            "scheduler_constructions": 4,
            "source_profiles": 2,
            "database_calls": 0,
            "exchange_calls": 0,
        } or not (
            isinstance(data["event_counts"], dict)
            and set(data["event_counts"]) == {"S6", "S8"}
            and all(type(v) is int and v > 0 for v in data["event_counts"].values())
        ):
            raise ValueError("INVALID_SMOKE_RESPONSE")
        return data
    except (OSError, subprocess.TimeoutExpired, ValueError, TypeError):
        return {"passed": False, "error_type": "PROBE_FAILED"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--entry", required=True, action="append")
    parser.add_argument("--smoke", choices=("controller", "none"), default="controller")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()
    try:
        closure = inspect_closure(args.candidate, args.reference, args.entry)
        smoke = (
            controller_smoke(args.candidate, python=args.python)
            if closure["passed"] and args.smoke == "controller"
            else {"performed": False, "reason": "NOT_REQUESTED_OR_CLOSURE_BLOCKED"}
        )
        result = {
            "read_only": True,
            "closure": closure,
            "callable_smoke": smoke,
            "passed": closure["passed"]
            and (args.smoke == "none" or smoke.get("passed") is True),
            "trading_authorized": False,
        }
    except Exception:  # noqa: BLE001 - never expose environment or child details
        result = {"read_only": True, "passed": False, "error_type": "PROBE_FAILED"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
