"""Bounded completed-request diagnostics, never a trading or quota decision.

The coordinator may publish this projection after a validated completion. Its
failure must not roll back that completion. Consumers read one exact primary
key; they do not scan model requests, prompts or business-state history.
"""

from v2_core.state import StateKey

MODEL_HEALTH_KEY = StateKey(
    "JEV",
    "shared-jev-primary",
    "GLOBAL",
    "ADVISORY",
    "jev-advisory-health-v1",
    "latest",
)
MODEL_HEALTH_POLICY_KEY = StateKey(
    "JEV",
    "shared-jev-primary",
    "GLOBAL",
    "ADVISORY",
    "jev-advisory-health-policy-v1",
    "current",
)
MODEL_HEALTH_SCHEMA = "jev-completed-model-health-v1"
MAX_RECENT_RESULTS = 20
MAX_CONSECUTIVE_FAILURES = 1000000
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_ACTIVITY_WINDOW_MS = 1800000
_MAX_TIME = 2**53 - 1
_FAILURE_REASONS = frozenset(
    {
        "MODEL_UNAVAILABLE",
        "MODEL_RESPONSE_INVALID",
        "MODEL_UNKNOWN_FAILURE",
        "MODEL_LATE_RESPONSE",
        "PROVIDER_REQUEST_REJECTED",
        "MODEL_CONFIGURATION_UNAVAILABLE",
        "MODEL_LOCAL_STARTUP_UNAVAILABLE",
        "MODEL_TIMEOUT",
        "TOKEN_RESERVATION_EXCEEDED",
        "DEADLINE_EXPIRED",
    }
)
_FIELDS = {
    "schema",
    "observed_at_ms",
    "last_success_at_ms",
    "last_failure_at_ms",
    "consecutive_failures",
    "recent_results",
    "execution_authorized",
}


def _integer(value, low=0, high=_MAX_TIME):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("JEV_MODEL_HEALTH_INVALID_INTEGER")
    return value


def _outcome(status, reason):
    if status == "ADVISORY_READY" and reason == "MODEL_OBSERVED":
        return True
    if status == "FALLBACK" and type(reason) is str and reason in _FAILURE_REASONS:
        return False
    raise ValueError("JEV_MODEL_HEALTH_INVALID_OUTCOME")


def _snapshot(value):
    if (
        type(value) is not dict
        or set(value) != _FIELDS
        or value["schema"] != MODEL_HEALTH_SCHEMA
        or value["execution_authorized"] is not False
    ):
        raise ValueError("JEV_MODEL_HEALTH_INVALID_SNAPSHOT")
    observed = _integer(value["observed_at_ms"])
    failures = _integer(value["consecutive_failures"], 0, MAX_CONSECUTIVE_FAILURES)
    last_success, last_failure = (
        value["last_success_at_ms"],
        value["last_failure_at_ms"],
    )
    for stamp in (last_success, last_failure):
        if stamp is not None:
            _integer(stamp, 0, observed)
    rows = value["recent_results"]
    if type(rows) is not list or not 1 <= len(rows) <= MAX_RECENT_RESULTS:
        raise ValueError("JEV_MODEL_HEALTH_INVALID_RESULTS")
    previous = 0
    trailing_failures = 0
    observed_success, observed_failure = None, None
    for row in rows:
        if type(row) is not dict or set(row) != {"completed_at_ms", "status", "reason"}:
            raise ValueError("JEV_MODEL_HEALTH_INVALID_RESULT")
        previous = _integer(row["completed_at_ms"], previous, observed)
        if _outcome(row["status"], row["reason"]):
            trailing_failures = 0
            observed_success = previous
        else:
            trailing_failures += 1
            observed_failure = previous
    if (
        previous != observed
        or (observed_success is not None and last_success != observed_success)
        or (observed_failure is not None and last_failure != observed_failure)
        or (
            observed_success is None
            and last_success is not None
            and last_success > rows[0]["completed_at_ms"]
        )
        or (
            observed_failure is None
            and last_failure is not None
            and last_failure > rows[0]["completed_at_ms"]
        )
        or failures < trailing_failures
        or (observed_success is not None and failures != trailing_failures)
        or (len(rows) < MAX_RECENT_RESULTS and failures != trailing_failures)
        or (rows[-1]["status"] == "ADVISORY_READY" and failures != 0)
    ):
        raise ValueError("JEV_MODEL_HEALTH_INCONSISTENT_SNAPSHOT")
    return value


def fold_model_health(previous, *, status, reason, completed_at_ms):
    """Pure bounded projection; only validated completed requests belong here.

    Missing means the projection has not been initialized. Corruption is not
    silently reset into apparent recovery. No request key, symbol, account,
    response text, token balance or prompt is stored in this shared diagnostic.
    """
    completed_at_ms = _integer(completed_at_ms)
    success = _outcome(status, reason)
    old = _snapshot(previous) if previous is not None else None
    if old is not None and completed_at_ms < old["observed_at_ms"]:
        raise ValueError("JEV_MODEL_HEALTH_CLOCK_REGRESSED")
    recent = [dict(row) for row in old["recent_results"]] if old is not None else []
    recent.append(
        {"completed_at_ms": completed_at_ms, "status": status, "reason": reason}
    )
    return {
        "schema": MODEL_HEALTH_SCHEMA,
        "observed_at_ms": completed_at_ms,
        "last_success_at_ms": completed_at_ms
        if success
        else old["last_success_at_ms"]
        if old
        else None,
        "last_failure_at_ms": completed_at_ms
        if not success
        else old["last_failure_at_ms"]
        if old
        else None,
        "consecutive_failures": 0
        if success
        else min(
            MAX_CONSECUTIVE_FAILURES, (old["consecutive_failures"] if old else 0) + 1
        ),
        "recent_results": recent[-MAX_RECENT_RESULTS:],
        "execution_authorized": False,
    }


def model_health_diagnostic(
    snapshot,
    *,
    now_ms,
    failure_threshold=DEFAULT_FAILURE_THRESHOLD,
    activity_window_ms=DEFAULT_ACTIVITY_WINDOW_MS,
    minimum_recent_requests=DEFAULT_FAILURE_THRESHOLD,
):
    """Fresh evidence, not provider billing, trade quality or execution health.

    Thresholds are explicit diagnostic arguments, independent of entry policy.
    No recent completed requests is IDLE, never proof that a failure recovered.
    Retained observations are not claimed to cover every provider request.
    """
    now_ms = _integer(now_ms)
    _integer(failure_threshold, 2, MAX_RECENT_RESULTS)
    _integer(minimum_recent_requests, 2, MAX_RECENT_RESULTS)
    _integer(activity_window_ms, 60000, 86400000)
    if snapshot is None:
        return {"status": "NO_OBSERVATIONS", "observed_at_ms": now_ms}
    value = _snapshot(snapshot)
    if value["observed_at_ms"] > now_ms:
        raise ValueError("JEV_MODEL_HEALTH_FUTURE_OBSERVATION")
    recent = [
        row
        for row in value["recent_results"]
        if now_ms - row["completed_at_ms"] < activity_window_ms
    ]
    successes = sum(row["status"] == "ADVISORY_READY" for row in recent)
    latest = value["recent_results"][-1]
    if not recent:
        status, reason = "IDLE", "NO_RECENT_COMPLETIONS"
    elif value["consecutive_failures"] >= failure_threshold:
        status, reason = "DEGRADED", "CONSECUTIVE_FAILURES"
    elif len(recent) >= minimum_recent_requests and not successes:
        status, reason = "DEGRADED", "RECENT_REQUESTS_WITHOUT_SUCCESS"
    elif latest["status"] == "ADVISORY_READY":
        status, reason = "HEALTHY", "LAST_COMPLETION_SUCCEEDED"
    else:
        status, reason = "WATCHING", "AWAITING_MORE_OBSERVATIONS"
    return {
        "status": status,
        "reason": reason,
        "observed_at_ms": now_ms,
        "latest_result_at_ms": value["observed_at_ms"],
        "last_success_at_ms": value["last_success_at_ms"],
        "last_failure_at_ms": value["last_failure_at_ms"],
        "consecutive_failures": value["consecutive_failures"],
        "recent_requests": len(recent),
        "recent_failures": len(recent) - successes,
        "window_ms": activity_window_ms,
        "latest_reason": latest["reason"],
        "execution_authorized": False,
    }


def model_health_policy(payload):
    """Optional dedicated diagnostic policy, never a trading-budget override."""
    values = {
        "failure_threshold": DEFAULT_FAILURE_THRESHOLD,
        "activity_window_ms": DEFAULT_ACTIVITY_WINDOW_MS,
        "minimum_recent_requests": DEFAULT_FAILURE_THRESHOLD,
    }
    if payload is not None:
        if type(payload) is not dict or not payload.keys() <= values.keys():
            raise ValueError("JEV_MODEL_HEALTH_POLICY_INVALID")
        values.update(payload)
    _integer(values["failure_threshold"], 2, MAX_RECENT_RESULTS)
    _integer(values["minimum_recent_requests"], 2, MAX_RECENT_RESULTS)
    _integer(values["activity_window_ms"], 60000, 86400000)
    return values
