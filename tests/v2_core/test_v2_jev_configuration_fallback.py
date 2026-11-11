"""Only a typed local client creation failure may use fresh baseline risk."""

import io
import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_v2_jev_entry_context import POLICY, SCOPE, binding, refresh
from test_v2_jev_entry_context import case as context_case_fixture
from test_v2_jev_public_context_v2 import features

from services import v2_jev_transport as transport
from services.v2_jev_coordinator import JevCoordinator
from services.v2_jev_entry_context import JevEntryContext
from services.v2_jev_reference import PUBLIC_SCOPE
from v2_core import jev_advisory as subject

context_case = context_case_fixture


@pytest.mark.parametrize(
    "kind,expected",
    [
        ("typed_creation", 6),
        ("ordinary_value", 1),
        ("named_lookalike", 1),
        ("missing_error_type", 7),
        ("typed_during_query", 1),
        ("invalid_200", 4),
    ],
)
def test_child_config_classification_is_typed_and_only_at_creation(
    monkeypatch, capsys, kind, expected
):
    calls = []

    class ConfigurationError(ValueError):
        pass

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def system_one(self, **kwargs):
            calls.append("system_one")
            if kind == "typed_during_query":
                raise ConfigurationError("SECRET_DO_NOT_EXPORT")
            return SimpleNamespace(model_dump=lambda **kw: {"SECRET": "invalid"})

    def create_client(**kwargs):
        calls.append("create_client")
        if kind == "typed_creation":
            raise ConfigurationError("SECRET_DO_NOT_EXPORT")
        if kind in {"ordinary_value", "missing_error_type"}:
            raise ValueError("api_key_not_configured SECRET_DO_NOT_EXPORT")
        if kind == "named_lookalike":
            raise type("ConfigurationError", (ValueError,), {})("SECRET")
        return Client()

    shared = SimpleNamespace(create_client=create_client)
    if kind != "missing_error_type":
        shared.ConfigurationError = ConfigurationError
    sdk = SimpleNamespace(
        RetryPolicy=lambda **kw: kw,
        **{
            name: type(name, (Exception,), {})
            for name in (
                "TypeSafeAPIError",
                "TypeSafeAPITimeoutError",
                "TypeSafeAPIConnectionError",
                "TypeSafeAPIResponseValidationError",
            )
        },
    )
    monkeypatch.setitem(sys.modules, "jev_shared", shared)
    monkeypatch.setitem(sys.modules, "typesafe_sdk", sdk)
    monkeypatch.setattr(transport, "version", lambda _: transport.SDK_VERSION)
    monkeypatch.setattr(transport.logging, "disable", lambda _: None)
    prepared = subject.prepare_request(features(), now_ms=1200, max_age_ms=60000)
    payload = json.dumps({"request": prepared["request"], "timeout_ms": 1000}).encode()
    output = io.BytesIO()
    monkeypatch.setattr(
        transport.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(payload))
    )
    monkeypatch.setattr(transport.sys, "stdout", SimpleNamespace(buffer=output))
    assert transport._sdk_once() == expected
    if kind == "missing_error_type":
        # An unavailable required local SDK type now fails during dependency
        # validation, before client creation or a potentially billable request.
        assert calls == []
        assert json.loads(output.getvalue()) == {
            "schema": "jev-local-startup-failure-v1",
            "stage": "SDK_IMPORT",
            "provider_request_started": False,
        }
    else:
        assert output.getvalue() == b""
        assert calls == (
            ["create_client", "system_one"]
            if kind in {"typed_during_query", "invalid_200"}
            else ["create_client"]
        )
    assert b"SECRET" not in output.getvalue()
    assert "SECRET" not in capsys.readouterr().err


@pytest.mark.parametrize("late", [False, True])
def test_exit_six_survives_durable_coordinator_and_falls_back_only_when_opted_in(
    context_case, late
):
    case = context_case

    def service(action):
        return JevEntryContext(
            case.db.connect,
            case.reference,
            scope=SCOPE,
            policy=replace(POLICY, unavailable_action=action),
        )

    case.service = service("BASELINE")
    assert case.service.assess(**case.args).status == "PENDING"
    _, original_binding, bound = binding(case)

    def query(request, timeout_ms):
        if late:
            case.db.now += timeout_ms
        # Exercise the real disposable-process protocol, without SDK/network.
        return transport._bounded_process(
            [sys.executable, "-I", "-c", "import sys;sys.exit(6)"], b"{}", 1000
        )

    result = subject.JevAdvisory(
        JevCoordinator(case.db.connect, scope=PUBLIC_SCOPE, enabled=True),
        query,
        policy=subject.AdvisoryPolicy(enabled=True),
        clock_ms=lambda: case.db.now,
    ).evaluate(bound["public_features"])
    assert result["status"] == "FALLBACK"
    assert result["reason"] == "MODEL_CONFIGURATION_UNAVAILABLE"
    assert result["hypotheses"] is None and result["usage"] is None
    refresh(case.args, case.db.now)
    assert service("SKIP").assess(**case.args).status == "SKIP"
    assessed = service("BASELINE").assess(**case.args)
    assert assessed.status == "FALLBACK"
    assert assessed.decision is case.args["decision"]
    evidence = json.loads(assessed.evidence_json)
    assert evidence["fallback_cause"] == "MODEL_CONFIGURATION_UNAVAILABLE"
    assert evidence["execution_authorized"] is False
    assert evidence["risk_override"] is False
    assert evidence["valid_until_ms"] <= case.args["context"]["valid_until_ms"]
    assert binding(case)[1] == original_binding and len(case.queued) == 1


@pytest.mark.parametrize("kind", ["ordinary_value", "invalid_200"])
def test_nonconfiguration_failures_still_skip_after_durable_completion(
    context_case, kind
):
    case = context_case
    case.service = JevEntryContext(
        case.db.connect,
        case.reference,
        scope=SCOPE,
        policy=replace(POLICY, unavailable_action="BASELINE"),
    )
    assert case.service.assess(**case.args).status == "PENDING"
    _, _, bound = binding(case)

    def query(request, timeout_ms):
        if kind == "ordinary_value":
            raise ValueError("api_key_not_configured SECRET_DO_NOT_EXPORT")
        return {"SECRET": "malformed successful response"}

    result = subject.JevAdvisory(
        JevCoordinator(case.db.connect, scope=PUBLIC_SCOPE, enabled=True),
        query,
        policy=subject.AdvisoryPolicy(enabled=True),
        clock_ms=lambda: case.db.now,
    ).evaluate(bound["public_features"])
    assert result["reason"] == (
        "MODEL_UNKNOWN_FAILURE"
        if kind == "ordinary_value"
        else "MODEL_RESPONSE_INVALID"
    )
    assert "SECRET" not in json.dumps(result)
    refresh(case.args, case.db.now)
    assert case.service.assess(**case.args).status == "SKIP"
