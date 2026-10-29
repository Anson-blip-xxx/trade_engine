"""Dashboard route contract without opening TCP sockets."""

import json
from io import BytesIO

from services.v2_dashboard import Handler


class FakeData:
    def overview(self, *, include_funnel=True):
        assert not include_funnel
        return {"summary": {"realized_pnl": "2.50"}, "trades": [], "signals": []}

    def funnel(self):
        return {"signal_funnel": [], "signal_funnel_status": "AVAILABLE"}

    def trade(self, trade_id):
        if trade_id == "00000000-0000-0000-0000-000000000001":
            return {"id": trade_id}
        return None


def request(path):
    handler = Handler.__new__(Handler)
    handler.path = path
    handler.data = FakeData()
    handler._send = lambda status, mime, body: (status, mime, body)
    return handler.do_GET()


def test_dashboard_serves_static_and_read_only_json():
    status, mime, body = request("/")
    assert status == 200 and mime.startswith("text/html")
    html = body.decode()
    assert 'data-page="dashboard"' in html
    assert "/accounts/workspace.js" in html and "/accounts/workspace.css" in html
    assert 'id="atlas-nav"' in html and 'id="atlas-header"' in html
    assert 'href="/accounts"' in html and 'data-workspace-path="/accounts"' in html
    assert 'id="account-alias"' in html and "实际执行账户" in html
    status, mime, body = request("/api/overview")
    assert status == 200 and mime == "application/json"
    assert json.loads(body)["summary"]["realized_pnl"] == "2.50"
    status, _, body = request("/api/signal-funnel")
    assert status == 200 and json.loads(body)["signal_funnel_status"] == "AVAILABLE"
    status, _, body = request("/api/trades/00000000-0000-0000-0000-000000000001")
    assert status == 200
    assert json.loads(body)["id"] == "00000000-0000-0000-0000-000000000001"


def test_dashboard_rejects_unknown_and_has_no_write_route():
    assert request("/api/trades/00000000-0000-0000-0000-000000000002")[0] == 404
    assert request("/etc/passwd")[0] == 404
    assert not hasattr(Handler, "do_POST")


def test_busy_analytics_does_not_occupy_all_dashboard_workers():
    slots = Handler._analytics_slots
    assert slots.acquire(blocking=False) and slots.acquire(blocking=False)
    try:
        assert request("/api/signal-funnel")[0] == 503
        assert request("/api/overview")[0] == 200
        assert request("/")[0] == 200
    finally:
        slots.release()
        slots.release()
    assert request("/api/signal-funnel")[0] == 200


def test_dashboard_sends_no_store_and_csp_headers():
    handler = Handler.__new__(Handler)
    handler.wfile = BytesIO()
    headers = {}
    handler.send_response = lambda status: headers.update(status=status)
    handler.send_header = lambda key, value: headers.update({key: value})
    handler.end_headers = lambda: None
    handler._send(200, "application/json", b"{}")
    assert headers["status"] == 200
    assert headers["Cache-Control"] == "no-store"
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert handler.wfile.getvalue() == b"{}"


def test_failures_log_only_safe_metadata_and_keep_public_error_generic(caplog):
    from psycopg.errors import QueryCanceled

    class BrokenData:
        def overview(self, **kwargs):
            raise QueryCanceled("password=secret-token SELECT private_account")

    handler = Handler.__new__(Handler)
    handler.path = "/api/overview?token=private-query-token"
    handler.data = BrokenData()
    handler._send = lambda status, mime, body: (status, mime, body)
    status, _, body = handler.do_GET()
    assert status == 503
    assert json.loads(body) == {"error": "DATA_UNAVAILABLE"}
    assert "QueryCanceled" in caplog.text and "57014" in caplog.text
    assert '"route": "/api/overview"' in caplog.text
    assert "request_id" in caplog.text and "frames" in caplog.text
    assert "secret-token" not in caplog.text
    assert "private-query-token" not in caplog.text
    assert "SELECT private_account" not in caplog.text
