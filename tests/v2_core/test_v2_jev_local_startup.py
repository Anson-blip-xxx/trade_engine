"""Known pre-request failure accounting, with no SDK, network or database."""

import json

import pytest

from tests.v2_core.test_v2_jev_advisory import (
    assert_advisory_only,
)
from tests.v2_core.test_v2_jev_advisory import (
    case as _case,
)
from tests.v2_core.test_v2_jev_advisory import (
    features as _features,
)
from v2_core import jev_advisory as core

case = _case
features = _features


@pytest.mark.parametrize("late", [False, True])
def test_explicit_pre_request_failure_releases_only_known_unused_tokens(
    features, case, late
):
    engine, coordinator, calls, clock = case

    def failed_startup(request, *, timeout_ms):
        calls.append("local-startup-attempt")
        if late:
            clock.now += timeout_ms
        raise core.JevLocalStartupUnavailable("private-must-not-escape")

    engine.query = failed_startup
    result = engine.evaluate(features)
    assert result["status"] == "FALLBACK"
    assert result["reason"] == "MODEL_LOCAL_STARTUP_UNAVAILABLE"
    assert result["hypotheses"] is None and result["usage"] is None
    assert coordinator.completions[0]["charged_input_tokens"] == 0
    assert "private" not in json.dumps(result)
    assert_advisory_only(result)
    engine.evaluate(features)
    assert calls == ["local-startup-attempt"]  # tombstone prevents blind retry


@pytest.mark.parametrize(
    "error,reason",
    [
        (ImportError, "MODEL_UNKNOWN_FAILURE"),
        (ModuleNotFoundError, "MODEL_UNKNOWN_FAILURE"),
        (ValueError, "MODEL_UNKNOWN_FAILURE"),
        (TimeoutError, "MODEL_TIMEOUT"),
        (core.JevProviderUnavailable, "MODEL_UNAVAILABLE"),
        (core.JevProviderRequestRejected, "PROVIDER_REQUEST_REJECTED"),
        (core.JevResponseInvalid, "MODEL_RESPONSE_INVALID"),
        (core.JevConfigurationUnavailable, "MODEL_CONFIGURATION_UNAVAILABLE"),
    ],
)
def test_unknown_or_provider_failures_never_get_startup_refund(
    features, case, error, reason
):
    engine, coordinator, _, _ = case

    def query(request, *, timeout_ms):
        raise error("private-must-not-escape")

    engine.query = query
    result = engine.evaluate(features)
    assert result["reason"] == reason
    assert coordinator.completions[0]["charged_input_tokens"] is None
    assert "private" not in json.dumps(result)
    assert_advisory_only(result)
