"""One-shot, controller-only SANDBOX release; dry run unless --execute.

No account identifiers or credentials are logged/persisted. Hub/admin release is
deliberately separate. Failure never forces ACTIVE, switches accounts, resets a
ledger, changes entry/exit policy, or silently rolls back a running process.
"""

import argparse
import json
import os
import subprocess
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path("/opt/trade-engine-v2")
LINK = ROOT / "execution-controller"
PRIOR = ROOT / "releases/jev-reentry-evidence-20261024-v1"
CANDIDATE = ROOT / "releases/profitability-foundation-controller-20261026-v1"
PYTHON = ROOT / "venvs/managed-controller-20260927/bin/python"
UNIT = "trade-v2-execution-controller.service"


# stdout from this child is captured by the parent and never forwarded. Scope
# information exists only in these two processes' memory and anonymous pipes.
DB_PROGRAM = r"""
import json, sys, time
from contextlib import nullcontext
from uuid import uuid4
import psycopg
from v2_core.execution_routes import ExecutionRoutes
from v2_core.state import StateKey

request = json.load(sys.stdin)
action, baseline = request["action"], request.get("baseline")
with psycopg.connect(
    "host=/var/run/postgresql port=55432 dbname=trade_v2_testnet user=tradev2accounts",
    connect_timeout=5,
    options="-c statement_timeout=5000 -c lock_timeout=1000 -c search_path=trade_v2,public",
) as c:
    c.execute("SET TRANSACTION READ WRITE" if action == "request" else "SET TRANSACTION READ ONLY")
    if action == "request":
        ExecutionRoutes._lock(c, baseline["tenant"], "SANDBOX")
    rows = c.execute(
        "SELECT r.tenant_id::text,r.target_registry::text,r.binding_version,r.epoch,r.phase,r.blockers,"
        "a.exchange,a.account_id,a.product,a.version "
        "FROM v2_execution_routes r JOIN v2_tenant_accounts a "
        "ON a.registry_id=r.target_registry AND a.tenant_id=r.tenant_id "
        "WHERE r.environment='SANDBOX' AND a.environment='SANDBOX' AND NOT EXISTS "
        "(SELECT 1 FROM v2_account_retirements x WHERE x.tenant_id=a.tenant_id AND x.registry_id=a.registry_id)"
    ).fetchall()
    if len(rows) != 1 or rows[0][2] != rows[0][9]:
        raise ValueError("UNIQUE_UNRETIRED_BINDING_REQUIRED")
    row = rows[0]
    current = dict(zip(("tenant","registry","binding","epoch","phase","blockers","exchange","account","product"), row[:9]))
    routes = ExecutionRoutes(lambda: nullcontext(c))
    observed = routes.inspect(current["tenant"], "SANDBOX")
    current["request_epoch"] = observed["request_epoch"]
    current["pending"] = observed["pending_request"] is not None
    if action == "capture":
        capture_phase = request.get("capture_phase", "ACTIVE")
        if capture_phase not in {"ACTIVE", "DRAINING"}:
            raise ValueError("INVALID_CAPTURE_PHASE")
        expected_blockers = [] if capture_phase == "ACTIVE" else ["RECOVERING_SOURCE"]
        if current["phase"] != capture_phase or current["blockers"] != expected_blockers or current["pending"]:
            raise ValueError("HEALTHY_ACTIVE_BASELINE_REQUIRED")
        if current["request_epoch"] != current["epoch"]:
            raise ValueError("NO_PENDING_REQUEST_REQUIRED")
        print(json.dumps(current))
    else:
        same = all(current[k] == baseline[k] for k in ("tenant","registry","binding","exchange","account","product"))
        if action == "request":
            if not same or current["phase"] != "DRAINING" or current["epoch"] != baseline["epoch"]:
                raise ValueError("SAME_BINDING_RECOVERY_REQUIRED")
            if current["pending"] or current["request_epoch"] != baseline["request_epoch"]:
                raise ValueError("CONCURRENT_ROUTE_REQUEST")
            requested = routes.request(
                baseline["tenant"], "SANDBOX", target_registry=baseline["registry"],
                binding_version=baseline["binding"], expected_epoch=baseline["request_epoch"],
                request_id=request["request_id"],
            )
            print(json.dumps({"epoch": requested["epoch"], "phase": requested["phase"]}))
        elif action == "status":
            coverage_key = StateKey(current["exchange"], current["account"], "SANDBOX", current["product"], "account-coverage-v1", "latest")
            coverage = c.execute(
                "SELECT b.payload->>'status', (extract(epoch FROM h.created_at)*1000)::bigint "
                "FROM v2_business_state b JOIN v2_state_history h ON h.state_id=b.state_id AND h.version=b.version "
                "WHERE b.state_id=%s AND NOT b.deleted",
                (coverage_key.identity,),
            ).fetchone()
            print(json.dumps({
                "same_binding": same, "epoch": current["epoch"], "phase": current["phase"],
                "blockers": current["blockers"], "pending": current["pending"],
                "request_epoch": current["request_epoch"],
                "coverage": coverage[0] if coverage else "UNKNOWN",
                "coverage_at_ms": coverage[1] if coverage else 0,
            }))
        else:
            raise ValueError("UNKNOWN_ACTION")
"""

IMPORT_PROGRAM = r"""
import importlib, inspect
from pathlib import Path
modules = (
    "services.v2_execution_supervisor", "services.v2_shared_risk_reference",
    "services.v2_s0_regime", "services.v2_profitability_audit",
    "services.v2_profit_capture_review", "services.v2_trend_runner_shadow",
    "v2_core.timeframe_indicators", "v2_core.trend_runner",
)
for name in modules:
    module = importlib.import_module(name)
    source = Path(module.__file__)
    assert source.resolve().is_relative_to(Path.cwd().resolve())
    compile(source.read_bytes(), str(source), "exec")
from services.v2_directional_exit_market import BinanceDirectionalExitMarket, LEGACY_EXIT_INDICATORS
assert inspect.signature(BinanceDirectionalExitMarket).parameters["indicator_version"].default == LEGACY_EXIT_INDICATORS
from v2_core.trend_runner import TrendRunnerPolicy
assert TrendRunnerPolicy().mode == "OFF"
print("IMPORTS_AND_SAFE_DEFAULTS_VERIFIED")
"""


class ReleaseError(RuntimeError):
    pass


def emit(event, **values):
    print(json.dumps({"event": event, **values}, separators=(",", ":")), flush=True)


def run(command, *, timeout=15, payload=None, cwd=None):
    """Never surface child stdout/stderr automatically, including exceptions."""
    try:
        result = subprocess.run(
            list(map(str, command)),
            input=payload,
            text=True,
            capture_output=True,
            timeout=timeout,
            cwd=cwd,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ReleaseError("COMMAND_UNAVAILABLE_OR_TIMEOUT") from None
    if result.returncode:
        raise ReleaseError("COMMAND_FAILED")
    return result.stdout


def database(action, baseline=None, **extra):
    output = run(
        ["sudo", "-n", "-u", "tradev2accounts", PYTHON, "-B", "-c", DB_PROGRAM],
        payload=json.dumps({"action": action, "baseline": baseline, **extra}),
        cwd=CANDIDATE,
        timeout=15,
    )
    try:
        result = json.loads(output)
        if not isinstance(result, dict):
            raise TypeError
        return result
    except (ValueError, TypeError):
        raise ReleaseError("INVALID_DATABASE_RESPONSE") from None


def unit_properties():
    output = run(
        [
            "systemctl",
            "show",
            "--no-pager",
            UNIT,
            "-p",
            "ActiveState",
            "-p",
            "SubState",
            "-p",
            "MainPID",
            "-p",
            "KillMode",
            "-p",
            "TimeoutStopUSec",
            "-p",
            "WorkingDirectory",
        ]
    )
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def verify_layout():
    for root in (PRIOR, CANDIDATE):
        if not root.is_dir() or root.is_symlink() or root.resolve() != root:
            raise ReleaseError("EXACT_RELEASE_DIRECTORY_REQUIRED")
    if not LINK.is_symlink() or LINK.resolve() != PRIOR:
        raise ReleaseError("UNEXPECTED_CURRENT_RELEASE")
    props = unit_properties()
    if props.get("ActiveState") != "active" or props.get("SubState") != "running":
        raise ReleaseError("RUNNING_CONTROLLER_REQUIRED")
    if props.get("KillMode") != "mixed" or props.get("TimeoutStopUSec") != "1min 30s":
        raise ReleaseError("BOUNDED_GRACEFUL_UNIT_REQUIRED")
    if props.get("WorkingDirectory") != str(LINK):
        raise ReleaseError("UNEXPECTED_WORKING_DIRECTORY")
    for legacy in (
        "trade-v2-testnet-daemon.service",
        "trade-v2-testnet-watchdog.service",
    ):
        if (
            run(["systemctl", "show", legacy, "-p", "ActiveState", "--value"]).strip()
            != "inactive"
        ):
            raise ReleaseError("LEGACY_EXECUTOR_MUST_BE_INACTIVE")
    result = run([PYTHON, "-B", "-c", IMPORT_PROGRAM], cwd=CANDIDATE, timeout=30)
    if result.strip() != "IMPORTS_AND_SAFE_DEFAULTS_VERIFIED":
        raise ReleaseError("CANDIDATE_IMPORT_CHECK_FAILED")


def journal_cursor():
    output = run(
        [
            "sudo",
            "-n",
            "journalctl",
            "-u",
            UNIT,
            "-n",
            "1",
            "--show-cursor",
            "--no-pager",
            "-o",
            "cat",
        ]
    )
    cursors = [
        line.removeprefix("-- cursor: ")
        for line in output.splitlines()
        if line.startswith("-- cursor: ")
    ]
    if len(cursors) != 1:
        raise ReleaseError("JOURNAL_CURSOR_REQUIRED")
    return cursors[0]


def graceful_stop_proved(output, *, since_us):
    stops = []
    for line in output.splitlines():
        try:
            journal = json.loads(line)
            if int(journal.get("__REALTIME_TIMESTAMP", 0)) < since_us:
                continue
            message = json.loads(journal.get("MESSAGE", ""))
        except (ValueError, TypeError, AttributeError):
            continue
        if (
            isinstance(message, dict)
            and message.get("component") == "execution-worker-stop"
        ):
            stops.append(message)
    return bool(stops) and all(
        item.get("forced") is False and item.get("stopped") is True for item in stops
    )


def replace_link():
    if not LINK.is_symlink() or LINK.resolve() != PRIOR:
        raise ReleaseError("RELEASE_CHANGED_DURING_STOP")
    temporary = ROOT / (".profitability-controller-" + uuid4().hex)
    run(["sudo", "-n", "ln", "-s", CANDIDATE, temporary])
    try:
        if not LINK.is_symlink() or LINK.resolve() != PRIOR:
            raise ReleaseError("RELEASE_CHANGED_DURING_SWITCH")
        run(["sudo", "-n", "mv", "-T", temporary, LINK])
    finally:
        if temporary.is_symlink() and temporary.resolve() == CANDIDATE:
            run(["sudo", "-n", "unlink", temporary])
    if LINK.resolve() != CANDIDATE:
        raise ReleaseError("CANDIDATE_LINK_NOT_ACTIVE")


def wait_route(baseline, *, phase, since_ms, timeout):
    deadline = time.monotonic() + timeout
    wanted_epoch = baseline["epoch"] + (phase == "ACTIVE")
    while time.monotonic() < deadline:
        observed = database("status", baseline)
        if not observed.get("same_binding"):
            raise ReleaseError("BINDING_CHANGED")
        # A request is expected to be pending while the controller activates.
        if (
            observed.get("request_epoch") != wanted_epoch or observed.get("pending")
        ) and not (phase == "ACTIVE" and observed.get("request_epoch") == wanted_epoch):
            raise ReleaseError("CONCURRENT_ROUTE_REQUEST")
        if observed.get("epoch", -1) > wanted_epoch:
            raise ReleaseError("ROUTE_EPOCH_ADVANCED_UNEXPECTEDLY")
        if (
            observed.get("phase") == phase
            and observed.get("epoch") == wanted_epoch
            and not observed.get("pending")
            and observed.get("coverage") == "ACCOUNT_COVERAGE_CLEAR"
            and observed.get("coverage_at_ms", 0) >= since_ms
            and (
                not observed.get("blockers")
                if phase == "ACTIVE"
                else observed.get("blockers") == ["RECOVERING_SOURCE"]
            )
        ):
            return observed
        time.sleep(min(2, max(0, deadline - time.monotonic())))
    raise ReleaseError("ROUTE_READINESS_TIMEOUT")


def execute(*, apply=False, resume=True):
    stage, baseline = "PRECHECK", None
    try:
        verify_layout()
        baseline = database("capture")
        emit(
            "PREFLIGHT_PASSED",
            environment="SANDBOX",
            epoch=baseline["epoch"],
            mutation_authorized=apply,
        )
        if not apply:
            return 0
        stage = "GRACEFUL_STOP"
        cursor = journal_cursor()
        stop_started_us = time.time_ns() // 1_000
        run(["sudo", "-n", "systemctl", "stop", UNIT], timeout=95)
        props = unit_properties()
        if props.get("ActiveState") != "inactive" or props.get("MainPID") != "0":
            raise ReleaseError("CONTROLLER_NOT_STOPPED")
        output = run(
            [
                "sudo",
                "-n",
                "journalctl",
                "-u",
                UNIT,
                "--after-cursor",
                cursor,
                "--no-pager",
                "-o",
                "json",
            ]
        )
        if not graceful_stop_proved(output, since_us=stop_started_us):
            raise ReleaseError("GRACEFUL_STOP_NOT_PROVEN")
        emit("GRACEFUL_STOP_VERIFIED", forced=False, stopped=True)
        stage = "SWITCH_RELEASE"
        replace_link()
        stage = "MANAGE_ONLY_RECOVERY"
        started_ms = time.time_ns() // 1_000_000
        run(["sudo", "-n", "systemctl", "start", UNIT], timeout=30)
        recovery = wait_route(
            baseline, phase="DRAINING", since_ms=started_ms, timeout=60
        )
        emit(
            "MANAGE_ONLY_VERIFIED",
            phase=recovery["phase"],
            coverage=recovery["coverage"],
        )
        if not resume:
            emit("MANAGE_ONLY_HELD", epoch=baseline["epoch"], entries_enabled=False)
            return 0
        stage = "SAME_BINDING_REQUEST"
        requested = database("request", baseline, request_id=str(uuid4()))
        if requested.get("epoch") != baseline["epoch"] + 1:
            raise ReleaseError("UNEXPECTED_REQUEST_EPOCH")
        stage = "ACTIVE_RECOVERY"
        active = wait_route(baseline, phase="ACTIVE", since_ms=started_ms, timeout=90)
        props = unit_properties()
        if props.get("ActiveState") != "active" or props.get("SubState") != "running":
            raise ReleaseError("CONTROLLER_NOT_RUNNING")
        emit(
            "CONTROLLER_RELEASE_ACTIVE",
            phase=active["phase"],
            epoch=active["epoch"],
            same_binding=True,
            coverage=active["coverage"],
            pid=int(props["MainPID"]),
            trading_profitability_verified=False,
        )
        return 0
    except Exception as exc:  # noqa: BLE001 - sanitize all unforeseen failures
        phase = "UNKNOWN"
        if baseline is not None:
            try:
                phase = database("status", baseline).get("phase", "UNKNOWN")
            except Exception:  # noqa: BLE001 - no account/error data in logs
                phase = "UNKNOWN"
        emit(
            "RELEASE_BLOCKED",
            stage=stage,
            code=str(exc) if isinstance(exc, ReleaseError) else "UNEXPECTED_FAILURE",
            actual_phase=phase,
            automatic_rollback=False,
        )
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Restart only the SANDBOX controller and request same-binding resume.",
    )
    args = parser.parse_args()
    return execute(apply=args.execute)


if __name__ == "__main__":
    raise SystemExit(main())
