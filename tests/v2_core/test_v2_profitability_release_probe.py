"""The deployment probe must stay read-only and never expose owner/account data."""

import json

import pytest

from scripts import v2_profitability_release_probe as module

REGISTRY = "00000000-0000-0000-0000-000000000001"
CONTEXT = {"pid": 123, "owner": "private-owner", "port": 1234}
NEW_JS = (
    module.BASIS + "T+60 相对全部平仓成交均价" + "非清仓后收益，不能据此判断卖飞"
).encode()


def fake(*, basis=module.BASIS, js=NEW_JS, unauthorized=403, error=None):
    calls = []

    def get(path, *, authenticated=True):
        calls.append((path, authenticated))
        if path == "/api/accounts/execution/SANDBOX":
            body = {"target_registry": REGISTRY}
        elif path.endswith("/strategy/review?days=1"):
            body = error or {
                "status": "AVAILABLE",
                "trades": [{"followup_60_basis": basis}],
                "limitations": ["旧 T+60 以所有平仓成交的加权均价为基准"],
            }
        elif path.endswith("/app.js"):
            return 200, js, 4
        elif path == "/api/accounts" and not authenticated:
            return unauthorized, b'{"error":"FORBIDDEN"}', 3
        else:
            raise AssertionError("UNEXPECTED_ENDPOINT")
        return 200, json.dumps(body).encode(), 2

    return get, calls


def test_release_requires_new_basis_and_uses_only_bounded_get_paths():
    get, calls = fake()
    result = module.probe(expect_new_basis=True, context=CONTEXT, fetch=get)
    assert result["passed"]
    assert len(calls) == 4
    assert result["review"]["known_basis_rows"] == 1
    serialized = json.dumps(result)
    assert REGISTRY not in serialized and CONTEXT["owner"] not in serialized
    assert calls[-1] == ("/api/accounts", False)


@pytest.mark.parametrize("basis", [None, "UNRECOGNIZED"])
def test_missing_or_unknown_basis_cannot_pass_release(basis):
    get, _ = fake(basis=basis)
    assert not module.probe(expect_new_basis=True, context=CONTEXT, fetch=get)["passed"]


def test_old_asset_allowed_only_for_baseline():
    for strict, expected in [(False, True), (True, False)]:
        get, _ = fake(basis=None, js="平仓后 60 分钟方向涨幅".encode())
        assert module.probe(expect_new_basis=strict, context=CONTEXT, fetch=get)["passed"] is expected


@pytest.mark.parametrize("code", [200, 401, 503])
def test_unauthenticated_request_must_be_forbidden(code):
    get, _ = fake(unauthorized=code)
    assert not module.probe(context=CONTEXT, fetch=get)["passed"]


def test_json_error_even_with_200_is_rejected():
    get, _ = fake(error={"error": "SENSITIVE_BACKEND_MESSAGE"})
    with pytest.raises(ValueError, match="INVALID_OR_ERROR_RESPONSE"):
        module.probe(context=CONTEXT, fetch=get)
    get, _ = fake(js=b'{"error":"SENSITIVE_BACKEND_MESSAGE"}')
    assert not module.probe(context=CONTEXT, fetch=get)["passed"]


def test_main_never_prints_exception_text_url_or_identity(monkeypatch, capsys):
    def fail(**_kwargs):
        raise ValueError("private-owner http://sensitive/" + REGISTRY)

    monkeypatch.setattr(module, "probe", fail)
    monkeypatch.setattr("sys.argv", ["probe"])
    assert module.main() == 1
    assert json.loads(capsys.readouterr().out) == {
        "read_only": True,
        "passed": False,
        "error_type": "ValueError",
    }


def test_listener_discovers_actual_loopback_port_without_importing_source():
    assert module.listener('Server(("127.0.0.1", 1234), handler)') == 1234


@pytest.mark.parametrize(
    "source",
    [
        'Server(("0.0.0.0", 1234), handler)',
        'Server(("127.0.0.1", 0), handler)',
        'Server(("127.0.0.1", True), handler)',
        'Server(("127.0.0.1", int(os.environ["PORT"])), handler)',
        'Server(("127.0.0.1", 1234), handler); Server(("127.0.0.1", 1235), handler)',
        "pass",
    ],
)
def test_unsafe_or_ambiguous_listener_rejected(source):
    with pytest.raises(ValueError):
        module.listener(source)
