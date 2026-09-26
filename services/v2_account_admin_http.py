"""Opt-in owner console handler; not mounted on the existing public dashboard.

Only a loopback trusted reverse proxy may set X-V2-Authenticated-User. It must
overwrite the client header with its authenticated identity. HTTPS origin is
configured server-side. Do not expose this handler directly or trust client tenant IDs.
"""

import json
import re
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

from v2_core.account_switches import AccountSwitches
from v2_core.credential_vault import VaultError

_ASSETS = Path(__file__).resolve().parents[1] / "web/v2_accounts"
_ACCOUNT = re.compile(r"^/api/accounts/([0-9a-f-]{36})/(overview|alias)$")


def handler_for(console, *, authenticated_user, origin):
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
                if path == "/api/accounts":
                    return self._send(200, {"accounts": console.accounts()})
                match = _ACCOUNT.fullmatch(path)
                if match and match[2] == "overview":
                    return self._send(200, console.overview(match[1]))
                assets = {
                    "/accounts": ("index.html", "text/html; charset=utf-8"),
                    "/accounts/app.js": ("app.js", "text/javascript; charset=utf-8"),
                    "/accounts/app.css": ("app.css", "text/css; charset=utf-8"),
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
            if (
                not self._authorized()
                or self.headers.get("Origin") != origin
                or self.headers.get("X-V2-Action") != "account-management"
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
                path = urlsplit(self.path).path
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
                    and match[2] == "alias"
                    and set(body) == {"alias", "expected_version", "request_id"}
                ):
                    return self._send(200, console.rename(match[1], **body))
                if path == "/api/account-switches" and set(body) == {
                    "source_registry",
                    "target_registry",
                    "request_id",
                }:
                    # Creates REQUESTED only. Never drains source or activates target.
                    result = AccountSwitches(console.connect).request(
                        console.tenant, **body
                    )
                    return self._send(202, result)
                return self._send(400, {"error": "INVALID_ACCOUNT_ACTION"})
            except (ValueError, TypeError, VaultError):
                return self._send(409, {"error": "ACCOUNT_ACTION_REJECTED"})
            except Exception:  # noqa: BLE001 - never reflect body or exception text
                return self._send(503, {"error": "ACCOUNT_SERVICE_UNAVAILABLE"})

    return AccountHandler
