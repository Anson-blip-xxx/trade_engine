"""R4B-PROTECTION-VERIFY guards for the pure exchange evidence core."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "position_protection/verification.py"
MODEL = ROOT / "docs/v2/P10_R4B_PROTECTION_VERIFICATION_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def test_only_new_and_working_are_active_exchange_protection():
    source = SOURCE.read_text()
    assert 'ACTIVE_EXCHANGE_PROTECTION_STATUSES = frozenset({"NEW", "WORKING"})' in source
    active = source.split("ACTIVE_EXCHANGE_PROTECTION_STATUSES", 1)[1].split(
        "VERIFYABLE_DESIRED_STATUSES", 1
    )[0]
    assert "TRIGGERED" not in active


def test_verification_requires_current_identity_exposure_alias_and_spec():
    source = SOURCE.read_text()
    for token in (
        "authority.status is AuthorityStatus.ACTIVE",
        "authority.episode_id == projection.episode_id == desired.episode_id",
        "projection.slot_generation",
        "desired.slot_generation",
        "exposure.quantity, projection.quantity",
        "exposure.quantity, desired.covered_quantity",
        "order.algo_alias in desired.exchange_algo_aliases",
        'order.order_type == "STOP_MARKET"',
        "order.reduce_only",
        "order.closing_side == expected_side == desired.closing_side",
    ):
        assert token in source


def test_evidence_is_fresh_and_multiple_active_stops_are_ambiguous():
    source = SOURCE.read_text()
    assert "required_after <= observed_at <= now" in source
    assert "now - observed_at <= max_age" in source
    assert "if len(active) != 1:" in source
    assert "another active STOP_MARKET order exists for the slot" in source


def test_verifier_is_pure_and_has_no_raw_exchange_or_storage_dependency():
    source = SOURCE.read_text()
    for forbidden in (
        "requests",
        "redis",
        "_light_fapi",
        "shared.position_manager",
        "allAlgoOrders",
        "openAlgoOrders",
    ):
        assert forbidden not in source


def test_verifier_remains_dormant_and_docs_preserve_commit_gate():
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_protection"):
            continue
        if "verify_current_protection" in path.read_text():
            callers.append(str(relative))
    assert callers == []
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    assert "It does not call Binance" in model
    assert "atomic verified-ACTIVE commit" in model
    assert "R4B remains `IN_PROGRESS`" in model
    assert "R4B-PROTECTION-VERIFY Strict Exchange Evidence Core | **IMPLEMENTED / CLOSED (DORMANT)**" in backlog
