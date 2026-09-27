from contextlib import contextmanager

import pytest

from v2_core.errors import SubmissionNotSent
from v2_core.guarded_transport import GuardedSignedRequest
from v2_core.transport import ExchangeTransportError


class Request:
    account_id = "qa"
    environment = "SANDBOX"

    def __init__(self, events):
        self.events = events

    def __call__(self, *args):
        self.events.append(("network", args))
        return {"ok": True}


def wrap(request, guard):
    return GuardedSignedRequest(
        request, account_id="qa", environment="SANDBOX", guard=guard
    )


@pytest.mark.parametrize(
    "method,path,params,operation",
    [
        ("POST", "/fapi/v1/order", {}, "OPEN"),
        ("POST", "/fapi/v1/order", {"reduceOnly": "false"}, "OPEN"),
        ("POST", "/fapi/v1/order", {"reduceOnly": "true"}, "MANAGE"),
        ("POST", "/fapi/v1/algoOrder", {"closePosition": "true"}, "MANAGE"),
        ("POST", "/fapi/v1/leverage", {}, "OPEN"),
        ("POST", "/fapi/v1/marginType", {}, "OPEN"),
        ("DELETE", "/fapi/v1/order", {}, "MANAGE"),
        ("DELETE", "/fapi/v1/algoOrder", {}, "MANAGE"),
    ],
)
def test_write_guard_spans_network(method, path, params, operation):
    events = []

    @contextmanager
    def guard(**kw):
        events.append(("enter", kw["operation"]))
        try:
            yield
        finally:
            events.append(("exit",))

    wrap(Request(events), guard)(method, path, params)
    assert (
        events[0] == ("enter", operation)
        and events[1][0] == "network"
        and events[2] == ("exit",)
    )


def test_fenced_write_is_proven_unsent_but_reads_continue():
    events = []

    @contextmanager
    def guard(**kw):
        raise RuntimeError("must not leak token")
        yield

    request = wrap(Request(events), guard)
    with pytest.raises(SubmissionNotSent, match="^EXECUTION_FENCED$"):
        request("POST", "/fapi/v1/order", {})
    assert not events
    request("GET", "/fapi/v1/order", {})
    assert len(events) == 1


def test_failure_after_send_never_reports_unsent():
    events = []

    @contextmanager
    def guard(**kw):
        yield
        raise RuntimeError("commit failed")

    with pytest.raises(ExchangeTransportError, match="NETWORK_OUTCOME_UNKNOWN"):
        wrap(Request(events), guard)("POST", "/fapi/v1/order", {})
    assert len(events) == 1


def test_vault_writer_cannot_skip_execution_binding():
    from types import SimpleNamespace

    from services.v2_testnet_daemon_entry import execution_guard_for

    config = SimpleNamespace(daemon=SimpleNamespace(write_enabled=True))
    with pytest.raises(ValueError, match="MANAGED_EXECUTION_BINDING_REQUIRED"):
        execution_guard_for(
            {"V2_CREDENTIAL_SOURCE": "vault"}, config=config, connect=lambda: None
        )
    with pytest.raises(ValueError, match="MANAGED_VAULT_MODE_REQUIRED"):
        execution_guard_for(
            {"V2_EXECUTION_EPOCH": "1"}, config=config, connect=lambda: None
        )
