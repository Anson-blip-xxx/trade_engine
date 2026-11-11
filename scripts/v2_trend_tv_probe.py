"""Bounded, non-admitting TradingView release probes; no trading capability.

By default only GET and unauthenticated empty JSON requests are sent. The
explicit --authenticated-version-proof option requires root and reads the
existing protected credential in memory. Those requests deliberately contain
invalid producer metadata and expired epoch-zero timestamps: they cannot be
admitted as a valid signal. No credential, response body or URL is printed.
"""

import argparse
import json
import os
import stat
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

SECRET_PATH = "/var/lib/trade-engine-v2/tradingview-webhook.secret"
ENDPOINTS = {
    "local_webhook": "http://127.0.0.1:18081/v2/webhooks/tradingview",
    "public_webhook": "https://43-133-4-231.sslip.io/v2/webhooks/tradingview",
    "public_health": "https://43-133-4-231.sslip.io/healthz",
}
MAX_BODY = 4096
TIMEOUT_SECONDS = 5


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def read_secret():
    """Root only, fixed protected regular file, finite read, no symlink follow."""
    if os.geteuid() != 0:
        raise PermissionError("ROOT_REQUIRED_FOR_AUTHENTICATED_PROOF")
    descriptor = os.open(
        SECRET_PATH, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    )
    try:
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or stat.S_IMODE(details.st_mode) not in {0o400, 0o440, 0o600}
            or not 32 <= details.st_size <= 513
        ):
            raise ValueError("PROTECTED_CREDENTIAL_REQUIRED")
        raw = os.read(descriptor, 514)
        if len(raw) != details.st_size:
            raise ValueError("CREDENTIAL_CHANGED")
    finally:
        os.close(descriptor)
    raw = raw[:-1] if raw.endswith(b"\n") else raw
    if not 32 <= len(raw) <= 512 or any(value < 32 or value > 126 for value in raw):
        raise ValueError("INVALID_CREDENTIAL")
    return raw.decode("ascii")


def negative_payload(secret):
    """The malformed producer is checked before any signal-store access."""
    return json.dumps(
        {
            "secret": secret,
            "event_id": "probe:tradingview:negative:producer-v2",
            "observed_at": 0,
            "expires_at_ms": 1,
            "symbol": "BINANCE:BTCUSDT.P",
            "signal": "TREND_UP_LONG",
            "price": "1",
            "strength": 0,
            "producer": {},
        },
        separators=(",", ":"),
    ).encode("utf-8")


def requester():
    # A credential must never follow a redirect or an environment-supplied proxy.
    opener = build_opener(ProxyHandler({}), NoRedirect())

    def fetch(endpoint, *, method, body=None):
        if (
            endpoint not in ENDPOINTS
            or method not in {"GET", "POST"}
            or (method == "GET" and body is not None)
            or (method == "POST" and (not isinstance(body, bytes) or len(body) > 2048))
        ):
            raise ValueError("BOUNDED_PROBE_REQUEST_REQUIRED")
        request = Request(
            ENDPOINTS[endpoint],
            data=body,
            headers={"Content-Type": "application/json"},
            method=method,
        )
        started = time.monotonic()
        try:
            response = opener.open(request, timeout=TIMEOUT_SECONDS)
        except HTTPError as exc:
            response = exc
        with response:
            data = response.read(MAX_BODY + 1)
            if len(data) > MAX_BODY:
                raise ValueError("RESPONSE_TOO_LARGE")
            return response.code, data, round((time.monotonic() - started) * 1000)

    return fetch


def probe(*, authenticated_version_proof=False, fetch=None, secret_loader=None):
    if authenticated_version_proof and os.geteuid() != 0:
        raise PermissionError("ROOT_REQUIRED_FOR_AUTHENTICATED_PROOF")
    fetch = requester() if fetch is None else fetch
    checks = [
        (
            "local_method",
            "local_webhook",
            "GET",
            None,
            405,
            {"error": "METHOD_NOT_ALLOWED"},
        ),
        (
            "local_auth",
            "local_webhook",
            "POST",
            b"{}",
            401,
            {"error": "AUTHENTICATION_FAILED"},
        ),
        (
            "public_health",
            "public_health",
            "GET",
            None,
            200,
            {"ok": True, "service": "trade-v2-edge"},
        ),
        (
            "public_auth",
            "public_webhook",
            "POST",
            b"{}",
            401,
            {"error": "AUTHENTICATION_FAILED"},
        ),
    ]
    if authenticated_version_proof:
        loader = read_secret if secret_loader is None else secret_loader
        body = negative_payload(loader())
        for scope in ("local", "public"):
            checks.append(
                (
                    f"{scope}_producer_version",
                    f"{scope}_webhook",
                    "POST",
                    body,
                    400,
                    {"error": "INVALID_PRODUCER_METADATA"},
                )
            )
    results = {}
    for name, endpoint, method, body, expected_status, expected_body in checks:
        try:
            status, response_body, elapsed = fetch(endpoint, method=method, body=body)
            try:
                matches = json.loads(response_body) == expected_body
            except (ValueError, UnicodeError, RecursionError):
                matches = False
            results[name] = {
                "http_status": status,
                "elapsed_ms": elapsed,
                "passed": status == expected_status and matches,
            }
        except Exception as exc:  # noqa: BLE001 - do not expose network/response content
            results[name] = {"passed": False, "error_type": type(exc).__name__}
    return {
        "read_only": True,
        "no_valid_signal_submitted": True,
        "authenticated_version_proof": authenticated_version_proof,
        "checks": results,
        "passed": all(result["passed"] for result in results.values()),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authenticated-version-proof", action="store_true")
    args = parser.parse_args()
    try:
        result = probe(authenticated_version_proof=args.authenticated_version_proof)
    except Exception as exc:  # noqa: BLE001 - never print secret-bearing exception text
        result = {
            "read_only": True,
            "no_valid_signal_submitted": True,
            "passed": False,
            "error_type": type(exc).__name__,
        }
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
