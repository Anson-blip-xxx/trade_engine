"""Opt-in owner console handler; not mounted on the existing public dashboard.

Only a loopback trusted reverse proxy may set X-V2-Authenticated-User. It must
overwrite the client header with its authenticated identity. HTTPS origin is
configured server-side. Do not expose this handler directly or trust client tenant IDs.
"""

import json
import re
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from v2_core.account_switches import AccountSwitches
from v2_core.credential_vault import VaultError

_ASSETS = Path(__file__).resolve().parents[1] / "web/v2_accounts"
_EXECUTION = re.compile(r"^/api/accounts/execution/(SANDBOX|LIVE)$")
_ACCOUNT = re.compile(
    r"^/api/accounts/([0-9a-f-]{36})/(overview|alias|verification|credentials)$"
)
_RECOVERY = re.compile(r"^/api/accounts/([0-9a-f-]{36})/risk-recovery$")
_STRATEGY = re.compile(
    r"^/api/accounts/([0-9a-f-]{36})/strategy/(policy|analytics|review|shadow|preview|apply)$"
)
_STRATEGY_ASSETS = Path(__file__).resolve().parents[1] / "web/v2_strategy"
_WORKSPACE_ASSETS = Path(__file__).resolve().parents[1] / "web/v2_workspace"


def handler_for(
    console,
    *,
    authenticated_user,
    origin,
    enable_switch_requests=False,
    verification=None,
    execution_routes=None,
    strategy_console=None,
    risk_recovery=None,
):
    parsed = urlsplit(origin)
    if (
        not authenticated_user
        or parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username
    ):
        raise ValueError("AUTHENTICATED_HTTPS_PROXY_REQUIRED")

    class AccountHandler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(10)
            super().setup()

        def log_message(self, _format, *_args):
            pass  # Never log bodies, URLs, credentials or aliases.

        def _send(self, status, payload, mime="application/json"):
            body = (
                json.dumps(payload, ensure_ascii=False).encode()
                if mime == "application/json"
                else payload
            )
            self.send_response(status)
            for key, value in {
                "Content-Type": mime,
                "Content-Length": str(len(body)),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
            }.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self):
            return (
                self.client_address[0] in {"127.0.0.1", "::1"}
                and self.headers.get("X-V2-Authenticated-User") == authenticated_user
            )

        def do_GET(self):
            if not self._authorized():
                return self._send(403, {"error": "FORBIDDEN"})
            path = urlsplit(self.path).path
            try:
                recovery = _RECOVERY.fullmatch(path)
                if recovery and risk_recovery is not None:
                    return self._send(200, risk_recovery.inspect(recovery[1]))
                if path in {"/accounts/workspace.css", "/accounts/workspace.js"}:
                    name = "app.css" if path.endswith(".css") else "app.js"
                    mime = (
                        "text/css; charset=utf-8"
                        if name.endswith(".css")
                        else "text/javascript; charset=utf-8"
                    )
                    return self._send(
                        200, (_WORKSPACE_ASSETS / name).read_bytes(), mime
                    )
                strategy = _STRATEGY.fullmatch(path)
                if strategy and strategy_console is not None:
                    if strategy[2] == "policy":
                        return self._send(200, strategy_console.inspect(strategy[1]))
                    if strategy[2] in {"analytics", "review", "shadow"}:
                        query = parse_qs(urlsplit(self.path).query, strict_parsing=True)
                        if set(query) - {"days"} or len(query.get("days", ["14"])) != 1:
                            raise ValueError("ANALYSIS_WINDOW_INVALID")
                        return self._send(
                            200,
                            getattr(strategy_console, strategy[2])(
                                strategy[1], days=int(query.get("days", ["14"])[0])
                            ),
                        )
                if strategy_console is not None and path in {
                    "/accounts/strategy",
                    "/accounts/strategy/",
                    "/accounts/strategy/app.js",
                    "/accounts/strategy/app.css",
                }:
                    name = path.rsplit("/", 1)[-1]
                    name = name if name in {"app.js", "app.css"} else "index.html"
                    mime = {
                        "index.html": "text/html; charset=utf-8",
                        "app.js": "text/javascript; charset=utf-8",
                        "app.css": "text/css; charset=utf-8",
                    }[name]
                    return self._send(200, (_STRATEGY_ASSETS / name).read_bytes(), mime)
                route = _EXECUTION.fullmatch(path)
                if route and execution_routes is not None:
                    return self._send(
                        200, execution_routes.inspect(console.tenant, route[1])
                    )
                if path == "/api/accounts":
                    return self._send(200, {"accounts": console.accounts()})
                match = _ACCOUNT.fullmatch(path)
                if match and match[2] == "overview":
                    return self._send(200, console.overview(match[1]))
                if match and match[2] == "verification" and verification is not None:
                    return self._send(200, verification.status(match[1]))
                assets = {
                    "/accounts": ("index.html", "text/html; charset=utf-8"),
                    "/accounts/": ("index.html", "text/html; charset=utf-8"),
                    "/accounts/app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "/accounts/app.css": ("app.css", "text/css; charset=utf-8"),
                    "/accounts/risk-recovery.js": (
                        "risk-recovery.js",
                        "text/javascript; charset=utf-8",
                    ),
                }
                if path in assets:
                    name, mime = assets[path]
                    return self._send(200, (_ASSETS / name).read_bytes(), mime)
                return self._send(404, {"error": "NOT_FOUND"})
            except (ValueError, VaultError):
                return self._send(404, {"error": "ACCOUNT_NOT_FOUND"})
            except Exception:  # noqa: BLE001 - no credential-bearing database diagnostics
                return self._send(503, {"error": "ACCOUNT_SERVICE_UNAVAILABLE"})

        def do_POST(self):
            self.close_connection = True
            strategy = _STRATEGY.fullmatch(urlsplit(self.path).path)
            if (
                not self._authorized()
                or self.headers.get("Origin") != origin
                or self.headers.get("X-V2-Action")
                != ("strategy-management" if strategy else "account-management")
            ):
                return self._send(403, {"error": "FORBIDDEN"})
            if self.headers.get(
                "Content-Type"
            ) != "application/json" or self.headers.get("Transfer-Encoding"):
                return self._send(415, {"error": "JSON_REQUIRED"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 8192:
                    return self._send(413, {"error": "BODY_LIMIT"})
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict):
                    raise TypeError()
                recovery = _RECOVERY.fullmatch(urlsplit(self.path).path)
                if recovery and risk_recovery is not None:
                    if set(body) != {"request_id", "proof", "expires_at_ms", "reason"}:
                        raise ValueError("RECOVERY_REQUEST_INVALID")
                    return self._send(200, risk_recovery.apply(recovery[1], **body))
                if strategy and strategy_console is not None:
                    if strategy[2] == "preview" and set(body) == {
                        "expected_version",
                        "changes",
                        "restore_version",
                    }:
                        return self._send(
                            200, strategy_console.preview(strategy[1], **body)
                        )
                    if strategy[2] == "apply" and set(body) == {
                        "expected_version",
                        "changes",
                        "restore_version",
                        "preview_digest",
                        "preview_expires_at_ms",
                        "reason",
                        "request_id",
                    }:
                        return self._send(
                            200, strategy_console.apply(strategy[1], **body)
                        )
                    return self._send(400, {"error": "INVALID_STRATEGY_ACTION"})
                path = urlsplit(self.path).path
                route = _EXECUTION.fullmatch(path)
                if (
                    route
                    and execution_routes is not None
                    and set(body)
                    == {
                        "target_registry",
                        "binding_version",
                        "expected_epoch",
                        "request_id",
                    }
                ):
                    return self._send(
                        202, execution_routes.request(console.tenant, route[1], **body)
                    )
                if path == "/api/accounts" and set(body) == {
                    "request_id",
                    "alias",
                    "environment",
                    "api_key",
                    "api_secret",
                }:
                    return self._send(201, console.add(**body))
                match = _ACCOUNT.fullmatch(path)
                if (
                    match
                    and match[2] == "credentials"
                    and set(body)
                    == {"request_id", "expected_version", "api_key", "api_secret"}
                ):
                    return self._send(200, console.rotate(match[1], **body))
                if (
                    match
                    and match[2] == "verification"
                    and verification is not None
                    and set(body) == {"expected_version"}
                ):
                    return self._send(200, verification.verify(match[1], **body))
                if (
                    match
                    and match[2] == "alias"
                    and set(body) == {"alias", "expected_version", "request_id"}
                ):
                    return self._send(200, console.rename(match[1], **body))
                if (
                    enable_switch_requests
                    and path == "/api/account-switches"
                    and set(body)
                    == {
                        "source_registry",
                        "target_registry",
                        "request_id",
                    }
                ):
                    # Creates REQUESTED only. Never drains source or activates target.
                    result = AccountSwitches(console.connect).request(
                        console.tenant, **body
                    )
                    return self._send(202, result)
                return self._send(400, {"error": "INVALID_ACCOUNT_ACTION"})
            except (ValueError, TypeError, VaultError) as exc:
                from services.v2_risk_recovery_console import ERRORS

                if _RECOVERY.fullmatch(urlsplit(self.path).path) and str(exc) in ERRORS:
                    return self._send(409, {"error": str(exc)})
                # Only constant, documented strategy errors may be reflected.
                safe = {
                    "POLICY_VERSION_CONFLICT",
                    "POLICY_PREVIEW_MISMATCH",
                    "POLICY_PREVIEW_EXPIRED",
                    "POLICY_REQUEST_CONFLICT",
                    "POLICY_REASON_REQUIRED",
                    "POLICY_FIELD_NOT_EDITABLE",
                    "POLICY_NO_CHANGE",
                    "POLICY_SANDBOX_ONLY",
                    "POLICY_RESERVE_30_PERCENT_REQUIRED",
                    "POLICY_SINGLE_MARGIN_EXCEEDS_POOL",
                    "POLICY_SINGLE_RISK_EXCEEDS_PORTFOLIO",
                    "POLICY_RESTORE_VERSION_INVALID",
                    "POLICY_REQUIRES_PROVISIONED_CAPITAL_RISK",
                }
                if strategy and str(exc) in safe:
                    return self._send(409, {"error": str(exc)})
                return self._send(409, {"error": "ACCOUNT_ACTION_REJECTED"})
            except Exception:  # noqa: BLE001 - never reflect body or exception text
                return self._send(503, {"error": "ACCOUNT_SERVICE_UNAVAILABLE"})

    return AccountHandler
