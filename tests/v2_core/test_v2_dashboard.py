"""Dashboard route contract without opening TCP sockets."""

import json
from io import BytesIO

from services.v2_dashboard import Handler


class FakeData:
    def overview(self):
        return {"summary": {"realized_pnl": "2.50"}, "trades": [], "signals": []}

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
    assert b"TRADING OVERVIEW" in body
    status, mime, body = request("/api/overview")
    assert status == 200 and mime == "application/json"
    assert json.loads(body)["summary"]["realized_pnl"] == "2.50"
    status, _, body = request("/api/trades/00000000-0000-0000-0000-000000000001")
    assert status == 200
    assert json.loads(body)["id"] == "00000000-0000-0000-0000-000000000001"


def test_dashboard_rejects_unknown_and_has_no_write_route():
    assert request("/api/trades/00000000-0000-0000-0000-000000000002")[0] == 404
    assert request("/etc/passwd")[0] == 404
    assert not hasattr(Handler, "do_POST")


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
