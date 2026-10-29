"""Actual Chromium rendering with local fake APIs; never touches trading endpoints."""

import json
import os
import signal
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CHROME = Path(
    "/home/ubuntu/.cache/ms-playwright/chromium_headless_shell-1243/chrome-headless-shell-linux64/chrome-headless-shell"
)
ACCOUNT = "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize("width", [390, 1440])
def test_real_chromium_panel_loads_and_fits_viewport(tmp_path, width):
    if not CHROME.is_file():
        pytest.skip("Chromium required for actual rendering QA")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            routes = {
                "/accounts": ("v2_accounts/index.html", "text/html"),
                "/accounts/app.js": ("v2_accounts/app.js", "text/javascript"),
                "/accounts/risk-recovery.js": (
                    "v2_accounts/risk-recovery.js",
                    "text/javascript",
                ),
                "/accounts/app.css": ("v2_accounts/app.css", "text/css"),
                "/accounts/workspace.js": ("v2_workspace/app.js", "text/javascript"),
                "/accounts/workspace.css": ("v2_workspace/app.css", "text/css"),
            }
            if self.path in routes:
                file, mime = routes[self.path]
                body = (ROOT / "web" / file).read_bytes()
                if self.path == "/accounts":
                    body += b"""<script>setInterval(()=>{
                      if(!document.getElementById('risk-form').hidden) {
                        document.body.dataset.recoveryLoaded='yes';
                        document.body.dataset.horizontalOverflow=String(document.documentElement.scrollWidth>innerWidth);
                      }
                    },100);</script>"""
            else:
                mime = "application/json"
                if self.path == "/api/accounts":
                    data = {
                        "accounts": [
                            {
                                "registry_id": ACCOUNT,
                                "alias": "QA Testnet",
                                "environment": "SANDBOX",
                                "status": "ENROLLED_UNVERIFIED",
                                "binding_version": 1,
                                "alias_version": 1,
                            }
                        ]
                    }
                elif self.path.endswith("/execution/SANDBOX"):
                    data = {
                        "phase": "ACTIVE",
                        "target_registry": ACCOUNT,
                        "epoch": 1,
                        "blockers": [],
                    }
                elif self.path.endswith("/overview"):
                    data = {
                        "registry_id": ACCOUNT,
                        "summary": {"trade_intents": 0, "settled_trades": 0},
                        "trades": [],
                    }
                elif self.path.endswith("/verification"):
                    data = {"outcome": "NOT_CHECKED"}
                elif self.path.endswith("/risk-recovery"):
                    data = {
                        "registry_id": ACCOUNT,
                        "eligible": True,
                        "blockers": [],
                        "proof": {},
                        "history": [],
                        "checked_at_ms": 1791619210000,
                        "probe_factor": ".2",
                        "loss_fraction": ".005",
                        "max_losses": 2,
                        "min_settlements": 3,
                        "active_drawdown": ".03",
                        "halt_drawdown": ".1",
                        "health": {
                            "base": "977",
                            "peak": "1040",
                            "drawdown": ".06",
                            "recovery": {"mode": "HALTED"},
                        },
                    }
                else:
                    self.send_error(404)
                    return
                body = json.dumps(data).encode()
            self.send_response(200)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        process = subprocess.Popen(
            [
                str(CHROME),
                "--headless",
                "--no-sandbox",
                "--disable-gpu",
                "--no-proxy-server",
                "--disable-background-networking",
                f"--user-data-dir={tmp_path / 'browser'}",
                f"--window-size={width},1000",
                "--virtual-time-budget=5000",
                "--dump-dom",
                f"http://127.0.0.1:{server.server_port}/accounts",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
            env={**os.environ, "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/dev/null"},
        )
        try:
            output, error = process.communicate(timeout=30)
            assert process.returncode == 0, error[-1000:]
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
        assert 'data-recovery-loaded="yes"' in output
        assert 'data-horizontal-overflow="false"' in output
        assert "确认恢复小风险试运行" in output
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
