"""D2C guards for strict Binance normalization and bounded reverification."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NORMALIZER = ROOT / "position_protection/binance_observation.py"
RETRY = ROOT / "position_protection/verification_retry.py"
MODEL = ROOT / "docs/v2/P10_D2C_BINANCE_VERIFICATION_ADAPTER_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def test_normalizer_requires_current_algo_schema_without_legacy_guessing():
    source = NORMALIZER.read_text()
    for field in (
        "algoId",
        "algoType",
        "orderType",
        "algoStatus",
        "triggerPrice",
        "quantity",
        "reduceOnly",
        "closePosition",
    ):
        assert f'"{field}"' in source
    for legacy in ("strategyId", "strategyStatus", "strategyType", "stopPrice"):
        assert legacy not in source


def test_query_completion_time_not_exchange_update_time_is_used():
    source = NORMALIZER.read_text()
    assert "observed_at=observed_at" in source
    assert "updateTime" not in source


def test_retry_is_bounded_by_attempts_deadline_and_delay_cap():
    source = RETRY.read_text()
    assert "attempts_completed >= policy.max_attempts" in source
    assert "now >= deadline" in source
    assert "policy.max_delay_seconds" in source
    assert "retry_at > deadline" in source
    assert "VerificationRetryAction.EXHAUSTED" in source


def test_only_observation_absence_inactivity_and_staleness_are_retryable():
    source = RETRY.read_text()
    retryable = source.split("_RETRYABLE =", 1)[1].split(
        "def decide_verification_retry", 1
    )[0]
    for code in ("STALE_EVIDENCE", "ORDER_NOT_FOUND", "ORDER_NOT_ACTIVE"):
        assert f"ProtectionVerificationCode.{code}" in retryable
    for code in ("IDENTITY_MISMATCH", "ORDER_SPEC_MISMATCH", "AMBIGUOUS_ACTIVE_ORDERS"):
        assert f"ProtectionVerificationCode.{code}" not in retryable


def test_modules_are_io_free_dormant_and_docs_preserve_gate():
    source = NORMALIZER.read_text() + RETRY.read_text()
    for forbidden in ("requests", "redis", "_light_fapi", "urllib", "httpx"):
        assert forbidden not in source
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_protection"):
            continue
        body = path.read_text()
        if "normalize_binance_verification_snapshot" in body or "decide_verification_retry" in body:
            callers.append(str(relative))
    assert callers == []
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    assert "No network\n> request or runtime caller is included" in model
    assert "R4B remains `IN_PROGRESS`" in model
    assert "D2C Binance Verification Adapter | **IMPLEMENTED / CLOSED (DORMANT)**" in backlog
