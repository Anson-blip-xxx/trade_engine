"""Read-only account-admin release probe; GET only, no stored credentials.

Run as a user permitted to read the account-admin process environment, normally
root. The trusted identity never leaves memory or appears in output. The listener
is discovered from the running process's entry module, without importing it.
Use --expect-new-basis to require the corrected review labels after deployment.
"""

import argparse
import ast
import json
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import UUID

UNIT = "trade-v2-account-admin.service"
BASIS = "ALL_CLOSE_FILL_VWAP_NOT_FINAL_FLAT_MARK"
MAX_BODY = 2_000_000


def main_pid():
    value = subprocess.check_output(
        ["systemctl", "show", "--no-pager", UNIT, "-p", "MainPID", "--value"],
        text=True,
        timeout=5,
        stderr=subprocess.DEVNULL,
    ).strip()
    if not value.isdecimal() or int(value) <= 1:
        raise ValueError("ACTIVE_ADMIN_PROCESS_REQUIRED")
    return int(value)


def listener(source):
    """Accept only the running entry module's unique literal loopback listener."""
    candidates = []
    for node in ast.walk(ast.parse(source)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Server"
            and node.args
        ):
            candidates.append(ast.literal_eval(node.args[0]))
    if len(candidates) != 1:
        raise ValueError("UNAMBIGUOUS_LISTENER_REQUIRED")
    value = candidates[0]
    if (
        not isinstance(value, tuple)
        or len(value) != 2
        or value[0] != "127.0.0.1"
        or type(value[1]) is not int
        or not 1 <= value[1] <= 65535
    ):
        raise ValueError("LITERAL_LOOPBACK_LISTENER_REQUIRED")
    return value[1]


def read_context():
    pid = main_pid()
    proc = Path("/proc") / str(pid)
    argv = (proc / "cmdline").read_bytes().split(b"\0")
    if b"services.v2_account_admin_entry" not in argv:
        raise ValueError("ADMIN_ENTRY_PROCESS_REQUIRED")
    owner = None
    # Read only the required field into a named value; never print the buffer.
    for item in (proc / "environ").read_bytes().split(b"\0"):
        if item.startswith(b"V2_ADMIN_OWNER="):
            owner = item.split(b"=", 1)[1].decode()
            break
    if not owner or any(ch in owner for ch in "\r\n\0"):
        raise ValueError("AUTHENTICATED_OWNER_REQUIRED")
    source = (proc / "cwd/services/v2_account_admin_entry.py").read_text()
    port = listener(source)
    if main_pid() != pid:
        raise RuntimeError("ADMIN_PROCESS_CHANGED")
    return {"pid": pid, "owner": owner, "port": port}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def getter(context):
    # Ignore proxy environment variables and never forward the trusted header.
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def get(path, *, authenticated=True):
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("LOCAL_PATH_REQUIRED")
        headers = (
            {"X-V2-Authenticated-User": context["owner"]} if authenticated else {}
        )
        request = Request(
            f"http://127.0.0.1:{context['port']}{path}",
            headers=headers,
            method="GET",
        )
        started = time.monotonic()
        try:
            response = opener.open(request, timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise ValueError("RESPONSE_TOO_LARGE")
            return response.code, data, round((time.monotonic() - started) * 1000)

    return get


def document(data):
    body = json.loads(data)
    if not isinstance(body, dict) or "error" in body:
        raise ValueError("INVALID_OR_ERROR_RESPONSE")
    return body


def probe(*, expect_new_basis=False, context=None, fetch=None):
    context = read_context() if context is None else context
    get = getter(context) if fetch is None else fetch
    result = {"read_only": True, "expect_new_basis": expect_new_basis}
    code, data, elapsed = get("/api/accounts/execution/SANDBOX")
    result["route"] = {"http_status": code, "elapsed_ms": elapsed}
    if code != 200:
        result["passed"] = False
        return result
    route = document(data)
    registry = str(UUID(route["target_registry"]))
    code, data, elapsed = get(
        f"/api/accounts/{registry}/strategy/review?days=1"
    )
    result["review"] = {"http_status": code, "elapsed_ms": elapsed}
    if code != 200:
        result["passed"] = False
        return result
    review = document(data)
    trades = review.get("trades")
    limitations = review.get("limitations")
    if (
        review.get("status") != "AVAILABLE"
        or not isinstance(trades, list)
        or not all(isinstance(row, dict) for row in trades)
        or not isinstance(limitations, list)
        or not all(isinstance(item, str) for item in limitations)
    ):
        raise ValueError("AVAILABLE_REVIEW_REQUIRED")
    known = sum(row.get("followup_60_basis") == BASIS for row in trades)
    missing = sum(row.get("followup_60_basis") is None for row in trades)
    explanation = any("所有平仓成交的加权均价" in item for item in limitations)
    result["review"].update(
        rows=len(trades),
        known_basis_rows=known,
        missing_basis_rows=missing,
        unknown_basis_rows=len(trades) - known - missing,
        has_basis_explanation=explanation,
    )
    code, data, elapsed = get("/accounts/strategy/app.js")
    new_labels = (
        BASIS.encode() in data
        and "T+60 相对全部平仓成交均价".encode() in data
        and "非清仓后收益，不能据此判断卖飞".encode() in data
    )
    old_label = "平仓后 60 分钟方向涨幅".encode() in data
    result["asset"] = {
        "http_status": code,
        "elapsed_ms": elapsed,
        "new_basis_labels": new_labels,
        "old_basis_label": old_label,
    }
    unauthorized, _, elapsed = get("/api/accounts", authenticated=False)
    result["unauthenticated"] = {
        "http_status": unauthorized,
        "elapsed_ms": elapsed,
    }
    result["passed"] = (
        code == 200
        and (new_labels or old_label)
        and unauthorized == 403
        and (
            not expect_new_basis
            or (known == len(trades) and explanation and new_labels and not old_label)
        )
    )
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-new-basis", action="store_true")
    args = parser.parse_args()
    try:
        result = probe(expect_new_basis=args.expect_new_basis)
    except Exception as exc:  # noqa: BLE001 - never expose sensitive exception text
        result = {"read_only": True, "passed": False, "error_type": type(exc).__name__}
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
