"""Pure, evidence-explicit research eligibility; never a trading release permit.

Times use half-open windows: train_end_ms is exclusive, and the test window is
[test_start_ms, test_end_ms). Counts describe unique settled OOS simulated trades
and independent test folds, not bars, overlapping variants or actual trade PnL.
"""

import re
from dataclasses import asdict, dataclass, fields
from decimal import Decimal

from v2_core.evidence import canonical, digest

SCHEMA_VERSION = "research-manifest-v1"
VERSION = "research-acceptance-v1"
_DIGEST = re.compile(r"[0-9a-f]{64}")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,119}")
_DECIMAL = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?")


def _number(value):
    if (
        not isinstance(value, str)
        or len(value) > 100
        or _DECIMAL.fullmatch(value) is None
    ):
        raise ValueError("RESEARCH_FINITE_DECIMAL_STRING_REQUIRED")
    number = Decimal(value)
    if not number.is_finite() or number.copy_abs() >= Decimal("1e30"):
        raise ValueError("RESEARCH_METRIC_OUT_OF_RANGE")
    return number


@dataclass(frozen=True)
class ResearchMetrics:
    realized_net_pnl: str | None = None
    max_drawdown_fraction: str | None = None
    cost_stress_net_pnl: str | None = None
    cost_stress_max_drawdown_fraction: str | None = None


@dataclass(frozen=True)
class ResearchManifest:
    # None is deliberately not a passing default: incomplete evidence is reported.
    schema_version: str | None = None
    run_id: str | None = None
    code_digest: str | None = None
    data_digest: str | None = None
    model_digest: str | None = None
    feature_version: str | None = None
    source_environment: str | None = None
    metrics_basis: str | None = None
    capital_scope: str | None = None
    historical_arrival_proven: bool | None = None
    historical_universe_proven: bool | None = None
    funding_complete: bool | None = None
    cost_stress_tested: bool | None = None
    independent_validation: bool | None = None
    search_trials: int | None = None
    train_end_ms: int | None = None
    test_start_ms: int | None = None
    test_end_ms: int | None = None
    sample_count: int | None = None
    fold_count: int | None = None
    metrics: ResearchMetrics | None = None


@dataclass(frozen=True)
class AcceptancePolicy:
    """Configurable research thresholds, with nonzero minimum evidence floors.

    These floors are engineering eligibility constraints, not statistical power
    calculations. A stricter strategy-specific policy may still be necessary.
    """

    min_oos_samples: int = 100
    min_folds: int = 3
    min_net_pnl: str = "0"
    max_drawdown_fraction: str = "0.20"
    required_source: str = "LIVE"

    def __post_init__(self):
        if (
            type(self.min_oos_samples) is not int
            or not 30 <= self.min_oos_samples <= 1000000
            or type(self.min_folds) is not int
            or not 2 <= self.min_folds <= 1000
            or self.min_folds > self.min_oos_samples
            or self.required_source not in ("LIVE", "SANDBOX")
        ):
            raise ValueError("RESEARCH_ACCEPTANCE_POLICY_INVALID")
        if (
            _number(self.min_net_pnl) < 0
            or not 0 < _number(self.max_drawdown_fraction) <= 1
        ):
            raise ValueError("RESEARCH_ACCEPTANCE_POLICY_INVALID")


def assess(manifest, *, policy=None):
    """Accept a typed manifest or its exact JSON-object shape; perform no I/O.

    Provenance hashes and boolean attestations are supplied by the caller. This
    function validates their contract, not their truth or the model's calibration.
    Performance threshold failures are RESEARCH_REJECTED; absent evidence remains
    INSUFFICIENT_EVIDENCE, while malformed values and leakage are INVALID_DATA.
    """
    policy = AcceptancePolicy() if policy is None else policy
    if not isinstance(policy, AcceptancePolicy):
        raise TypeError("RESEARCH_ACCEPTANCE_POLICY_REQUIRED")
    checks, values, numbers = [], {}, {}

    def record(name, status, reason):
        checks.append({"name": name, "status": status, "reason": reason})

    def required(name, value, valid):
        if value is None:
            record(name, "MISSING", "REQUIRED_EVIDENCE_MISSING")
            return False
        if not valid(value):
            record(name, "INVALID", "INVALID_FIELD_VALUE")
            return False
        values[name] = value
        record(name, "PASSED", "VALID_FIELD")
        return True

    if isinstance(manifest, ResearchManifest):
        payload = asdict(manifest)
    elif isinstance(manifest, dict):
        payload = dict(manifest)
    else:
        payload = {}
        record("manifest", "INVALID", "MANIFEST_OBJECT_REQUIRED")
    allowed = {f.name for f in fields(ResearchManifest)}
    if set(payload) - allowed:
        record("manifest_fields", "INVALID", "UNKNOWN_MANIFEST_FIELDS")
    for name, valid in (
        ("schema_version", lambda x: x == SCHEMA_VERSION),
        ("run_id", lambda x: isinstance(x, str) and _IDENTITY.fullmatch(x) is not None),
        (
            "code_digest",
            lambda x: isinstance(x, str) and _DIGEST.fullmatch(x) is not None,
        ),
        (
            "data_digest",
            lambda x: isinstance(x, str) and _DIGEST.fullmatch(x) is not None,
        ),
        (
            "feature_version",
            lambda x: isinstance(x, str) and _IDENTITY.fullmatch(x) is not None,
        ),
        ("source_environment", lambda x: x in ("LIVE", "SANDBOX")),
        ("metrics_basis", lambda x: x == "SIMULATED_OOS"),
        ("capital_scope", lambda x: x in ("SINGLE_SYMBOL", "PORTFOLIO")),
    ):
        required(name, payload.get(name), valid)
    if payload.get("model_digest") is not None:
        required(
            "model_digest",
            payload["model_digest"],
            lambda x: isinstance(x, str) and _DIGEST.fullmatch(x) is not None,
        )
    for name in (
        "historical_arrival_proven",
        "historical_universe_proven",
        "funding_complete",
        "cost_stress_tested",
        "independent_validation",
    ):
        value = payload.get(name)
        if value is False:
            record(name, "MISSING", "REQUIRED_EVIDENCE_NOT_ESTABLISHED")
        else:
            required(name, value, lambda x: type(x) is bool and x is True)
    for name, lower, upper in (
        ("search_trials", 1, 1000000),
        ("sample_count", 0, 1000000000),
        ("fold_count", 0, 1000000),
        ("train_end_ms", 0, 2**53 - 1),
        ("test_start_ms", 0, 2**53 - 1),
        ("test_end_ms", 0, 2**53 - 1),
    ):
        required(
            name,
            payload.get(name),
            lambda x, low=lower, high=upper: type(x) is int and low <= x <= high,
        )
    if all(k in values for k in ("train_end_ms", "test_start_ms", "test_end_ms")):
        if values["train_end_ms"] > values["test_start_ms"]:
            record("time_separation", "INVALID", "TRAIN_TEST_OVERLAP")
        elif values["test_start_ms"] >= values["test_end_ms"]:
            record("time_separation", "INVALID", "TEST_WINDOW_EMPTY_OR_REVERSED")
        else:
            record("time_separation", "PASSED", "HALF_OPEN_WINDOWS_SEPARATED")
    if "source_environment" in values:
        record(
            "required_source",
            "PASSED"
            if values["source_environment"] == policy.required_source
            else "MISSING",
            "SOURCE_MATCHED"
            if values["source_environment"] == policy.required_source
            else "REQUIRED_SOURCE_EVIDENCE_MISSING",
        )
    for name, minimum in (
        ("sample_count", policy.min_oos_samples),
        ("fold_count", policy.min_folds),
    ):
        if name in values:
            record(
                "minimum_" + name,
                "PASSED" if values[name] >= minimum else "MISSING",
                "SAMPLE_REQUIREMENT_MET"
                if values[name] >= minimum
                else "INSUFFICIENT_OOS_SAMPLES"
                if name == "sample_count"
                else "INSUFFICIENT_OOS_FOLDS",
            )
    metrics = payload.get("metrics")
    if isinstance(metrics, ResearchMetrics):
        metrics = asdict(metrics)
    metric_names = {f.name for f in fields(ResearchMetrics)}
    if metrics is not None and not isinstance(metrics, dict):
        record("metrics", "INVALID", "METRICS_OBJECT_REQUIRED")
        metrics = {}
    elif metrics is None:
        metrics = {}
    if set(metrics) - metric_names:
        record("metric_fields", "INVALID", "UNKNOWN_METRIC_FIELDS")
    for name in sorted(metric_names):
        value = metrics.get(name)
        if value is None:
            record(name, "MISSING", "REQUIRED_METRIC_MISSING")
            continue
        try:
            number = _number(value)
            if "drawdown" in name and not 0 <= number <= 1:
                raise ValueError("DRAWDOWN_FRACTION_INVALID")
        except ValueError:
            record(name, "INVALID", "INVALID_FINITE_METRIC")
            continue
        numbers[name] = value
        meets = (
            number <= _number(policy.max_drawdown_fraction)
            if "drawdown" in name
            else number >= _number(policy.min_net_pnl)
        )
        record(
            name,
            "PASSED" if meets else "FAILED",
            "PERFORMANCE_THRESHOLD_MET"
            if meets
            else "DRAWDOWN_LIMIT_EXCEEDED"
            if "drawdown" in name
            else "NET_PNL_BELOW_MINIMUM",
        )
    states = {check["status"] for check in checks}
    status = (
        "INVALID_DATA"
        if "INVALID" in states
        else "INSUFFICIENT_EVIDENCE"
        if "MISSING" in states
        else "RESEARCH_REJECTED"
        if "FAILED" in states
        else "RESEARCH_PASSED"
    )
    # Only valid exact-JSON input can receive a reproducibility fingerprint.
    fingerprint = None
    if "INVALID" not in states:
        normalized = {name: payload.get(name) for name in sorted(allowed)}
        normalized["metrics"] = metrics
        fingerprint = digest(canonical(normalized))
    return {
        "version": VERSION,
        "purpose": "OFFLINE_OOS_RESEARCH_ELIGIBILITY_NOT_RELEASE_APPROVAL",
        "status": status,
        "run_id": values.get("run_id"),
        "manifest_digest": fingerprint,
        "policy_digest": digest(canonical(asdict(policy))),
        "policy": asdict(policy),
        "metrics_basis": values.get("metrics_basis"),
        "capital_scope": values.get("capital_scope"),
        "source_environment": values.get("source_environment"),
        "metrics": numbers,
        "checks": checks,
        "blockers": [check for check in checks if check["status"] != "PASSED"],
        "model_calibration_status": "NOT_ASSESSED",
        "execution_authorized": False,
        "automatic_rollout": False,
        "limitations": [
            "OOS results and passing thresholds do not guarantee future profits.",
            "Missing evidence never defaults to passed; no sample values are imputed.",
            "Digests establish reproducibility, not truthful data, valid calibration or model quality.",
            "Evidence flags are caller attestations; source artifacts and fold membership are not inspected.",
            "Aggregate OOS metrics do not prove independence, per-fold stability or adequate statistical power.",
            "Actual settled trade statistics are retrospective facts, not simulated OOS validation.",
            "Single-symbol sleeves cannot be summed as a shared-capital portfolio.",
            "Research eligibility does not authorize orders, configuration changes or deployment.",
        ],
    }
