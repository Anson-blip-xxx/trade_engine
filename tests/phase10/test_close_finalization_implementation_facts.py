"""R4A-CLOSE architecture guards for canonical ACTIVE-to-FLAT release."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "docs/v2/P10_R4A_CLOSE_FINALIZATION_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def _function(source, start, end):
    return source.split(start, 1)[1].split(end, 1)[0]


def test_close_fence_contains_all_aba_guards():
    source = (ROOT / "position_identity/close_finalizer.py").read_text()
    for field in (
        "episode_id: str",
        "slot_generation: int",
        "authority_revision: int",
        "projection_revision: int",
    ):
        assert field in source
    assert "expected_episode_id=fence.episode_id" in source
    assert "expected_slot_generation=fence.slot_generation" in source
    assert "expected_revision=fence.authority_revision" in source


def test_capture_requires_native_active_authority_and_matching_projection():
    source = (ROOT / "position_identity/close_finalizer.py").read_text()
    body = _function(source, "def _matches(", "\n        )")
    for guard in (
        "authority.status is AuthorityStatus.ACTIVE",
        "authority.provenance is AuthorityProvenance.NATIVE",
        "authority.episode_id == projection.episode_id",
        "authority.slot_generation == projection.slot_generation",
        "projection.side == local_side",
        "projection.system == local_system",
    ):
        assert guard in body


def test_lifecycle_releases_only_exchange_flat_success_paths():
    source = (ROOT / "position_lifecycle/service.py").read_text()
    close = _function(source, "def close(", "\n    def partial_close(")
    assert close.count("self._finalize_close_fence(symbol, close_fence)") == 2
    assert close.index("if remaining_qty >= 0.001:") < close.rindex(
        "self._finalize_close_fence(symbol, close_fence)"
    )
    partial = _function(source, "def partial_close(", "\n    def open_position(")
    assert "_finalize_close_fence" not in partial


def test_ghost_and_reconcile_require_observed_exchange_absence():
    source = (ROOT / "position_reconcile/service.py").read_text()
    ghost = _function(source, "def ghost_cleanup(", "\n    def ghost_cleanup_one(")
    reconcile = _function(source, "def reconcile_all(", "\n    def notify_external_position(")
    assert "if sym in real_syms:" in ghost
    assert "self._finalize_close_fence(sym, close_fence)" in ghost
    assert "if sym not in real_positions:" in reconcile
    assert "self._finalize_close_fence(sym, close_fence)" in reconcile


def test_runtime_wiring_requires_explicit_principal_and_strict_redis():
    source = (ROOT / "shared/position_manager.py").read_text()
    slot = _function(source, "def _canonical_close_slot(", "\n\ndef _capture_canonical_close(")
    service = _function(source, "def _canonical_close_service(", "\n\ndef _canonical_close_slot(")
    assert "ACCOUNT_PRINCIPAL_ID" in slot
    assert "if not principal:" in slot
    assert "redis_get=_strict_redis_get" in service
    assert "redis_eval=_strict_redis_eval" in service
    assert "ccap=_capture_canonical_close" in source
    assert "cfin=_finalize_canonical_close" in source


def test_docs_close_only_r4a_close_not_all_r4():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    assert "**IMPLEMENTED / CLOSED on isolated V2.**" in model
    assert "R4A-CLOSE Canonical Close Finalization | **IMPLEMENTED / CLOSED**" in backlog
    assert "R4 Active Producer Wiring | IN_PROGRESS" in backlog
