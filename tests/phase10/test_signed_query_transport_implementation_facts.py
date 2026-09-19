"""D2C-TRANSPORT guards for injected signed verification queries."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "position_protection/binance_query.py"
MODEL = ROOT / "docs/v2/P10_D2C_SIGNED_QUERY_TRANSPORT_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def test_transport_has_no_http_credentials_or_endpoint_defaults():
    source = SOURCE.read_text()
    for forbidden in (
        "requests",
        "httpx",
        "hmac",
        "API_KEY",
        "API_SECRET",
        '"/fapi',
        '"/papi',
    ):
        assert forbidden not in source
    assert "signed_get:" in source
    assert "endpoints: BinanceVerificationEndpoints" in source


def test_both_queries_are_symbol_scoped_and_clock_is_sampled_afterward():
    source = SOURCE.read_text()
    position = source.index("self._endpoints.position_risk_path")
    algo = source.index("self._endpoints.open_algo_orders_path")
    clock = source.index("observed_at = self._clock()")
    assert position < algo < clock
    assert 'self._signed_get(path, {"symbol": symbol})' in source


def test_query_failures_are_stage_typed_and_non_lists_rejected():
    source = SOURCE.read_text()
    assert 'POSITION_QUERY_FAILED = "POSITION_QUERY_FAILED"' in source
    assert 'ALGO_QUERY_FAILED = "ALGO_QUERY_FAILED"' in source
    assert "if not isinstance(payload, list):" in source
    assert 'payload.get("code")' in source
    assert 'payload.get("msg")' in source


def test_transport_remains_dormant_and_docs_preserve_endpoint_gate():
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_protection"):
            continue
        if "InjectedBinanceVerificationQueryAdapter" in path.read_text():
            callers.append(str(relative))
    assert callers == []
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    assert "provides no `/fapi` or `/papi` default" in model
    assert "R4B remains `IN_PROGRESS`" in model
    assert "D2C Signed Query Transport | **IMPLEMENTED / CLOSED (DORMANT)**" in backlog
