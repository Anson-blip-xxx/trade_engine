"""P10-07D architecture guards for dormant episode onboarding."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = ROOT / 'position_identity'
ADOPTION = PACKAGE / 'adoption.py'
MODEL = ROOT / 'docs/v2/P10_D5A3_EPISODE_ONBOARDING_IMPLEMENTATION.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'


def _imported_modules(path):
    tree = ast.parse(path.read_text())
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_adoption_module_exists_and_is_a_leaf_domain_service():
    assert ADOPTION.is_file()
    imported = _imported_modules(ADOPTION)
    forbidden_roots = {
        'shared', 'strategies', 'execution', 'position_lifecycle',
        'position_monitoring', 'position_protection', 'position_reconcile',
        'position_runtime', 'position_state', 'redis', 'requests',
    }
    assert {module.split('.')[0] for module in imported}.isdisjoint(
        forbidden_roots)
    assert 'position_identity.authority_redis' not in imported


def test_onboarding_has_no_scan_position_write_or_file_fallback_capability():
    source = ADOPTION.read_text()
    for token in (
        'scan_all_legacy_positions', 'positionRisk', 'pm:positions',
        'redis_store', 'Path(', 'open(', 'write_text', 'read_text',
        'file fallback', 'KEY_MAP',
    ):
        assert token not in source


def test_reconstruction_is_only_reconstructed_quarantined():
    source = ADOPTION.read_text()
    assert 'AuthorityProvenance.RECONSTRUCTED' in source
    assert 'status=AuthorityStatus.QUARANTINED' in source
    assert 'create_reconstructed_quarantined(' in source
    reconstruction = source.split(
        'def apply_reconstructed_episode(', 1)[1].split(
            'def _reconstruction_cas_resolution(', 1)[0]
    assert 'adopt_if_unowned(' not in reconstruction
    assert 'allocate_new_episode(' not in reconstruction


def test_onboarding_has_no_protection_or_close_side_effects():
    source = ADOPTION.read_text().lower()
    for token in (
        'enqueue', 'algo_sl', 'create_sl', 'cancel_order', 'cancel_algo',
        'protectionservice', 'close_position', 'partial_close', 'reduceonly',
    ):
        assert token not in source


def test_active_mutation_predicate_is_complete_and_fail_closed():
    source = ADOPTION.read_text()
    predicate = source.split('def can_mutate_async(', 1)[1]
    for token in (
        'AuthorityStatus.ACTIVE', 'authority.episode_id',
        'authority.slot_generation > 0', 'authority.provenance is not None',
    ):
        assert token in predicate
    assert 'AuthorityStatus.QUARANTINED' not in predicate


def test_plans_and_authority_provenance_are_immutable():
    adoption = ADOPTION.read_text()
    authority = (PACKAGE / 'authority.py').read_text()
    for class_name in (
        'ClassificationDecision', 'AuthorityExpectation', 'AdoptionPlan',
        'ReconstructionPlan', 'OnboardingResult',
    ):
        assert f'@dataclass(frozen=True)\nclass {class_name}:' in adoption
    assert '@dataclass(frozen=True)\nclass SlotAuthority:' in authority
    assert 'provenance=self.provenance' in authority


def test_legacy_alias_is_preserved_but_not_used_as_episode_identity():
    source = ADOPTION.read_text()
    assert 'legacy_position_id_alias=plan.legacy_position_id_alias' in source
    assert 'candidate_episode_id=plan.candidate_episode_id' in source
    assert 'candidate_episode_id=plan.legacy_position_id_alias' not in source


def test_backend_failure_remains_typed_without_business_fallback():
    source = ADOPTION.read_text()
    assert 'OnboardingResultCode.BACKEND_ERROR' in source
    assert 'QuarantineReason.BACKEND_UNAVAILABLE' in source
    assert 'AuthorityReadCode.BACKEND_ERROR' in source
    assert 'fallback' not in source.lower()


def test_no_active_production_caller_imports_onboarding():
    callers = []
    symbols = (
        'position_identity.adoption', 'prepare_legacy_adoption',
        'apply_legacy_adoption', 'prepare_reconstructed_episode',
        'apply_reconstructed_episode', 'can_mutate_async',
    )
    for path in ROOT.rglob('*.py'):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ('tests', 'position_identity'):
            continue
        source = path.read_text()
        if any(symbol in source for symbol in symbols):
            callers.append(str(relative))
    assert callers == ['position_protection/fence.py']


def test_emergency_close_paths_remain_authority_independent():
    for relative in (
        'strategies/shared_executor.py',
        'shared/position_manager.py',
        'position_lifecycle/service.py',
        'execution/service.py',
    ):
        source = (ROOT / relative).read_text()
        assert 'position_identity.adoption' not in source
        assert 'apply_legacy_adoption' not in source
        assert 'can_mutate_async' not in source


def test_startup_monitor_reconcile_and_worker_have_no_onboarding_wiring():
    for relative in (
        'strategies/S6.py', 'strategies/S8.py',
        'position_monitoring/service.py', 'position_reconcile/service.py',
        'position_runtime/runtime.py', 'position_state/service.py',
    ):
        imported = _imported_modules(ROOT / relative)
        assert 'position_identity.adoption' not in imported


def test_position_payload_unchanged_and_queue_identity_extended():
    executor = (ROOT / 'strategies/shared_executor.py').read_text()
    cache = executor.split('def _update_pos_cache(', 1)[1].split(
        'def _get_positions(', 1)[0]
    for token in ('episode_id', 'slot_generation', 'identity_provenance'):
        assert token not in cache
    pm = (ROOT / 'shared/position_manager.py').read_text()
    runtime = (ROOT / 'position_runtime/runtime.py').read_text()
    assert '_ALGO_QUEUE.append(task)' in pm
    assert 'parsed = classify_queue_task(task)' in runtime


def test_implementation_doc_closes_d5a_and_records_d3d1c_progress():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    for phrase in (
        'LEGACY_ACTIVE', 'RECONSTRUCTED', 'MIGRATED', 'QUARANTINED',
        '`can_mutate_async`', 'emergency close', 'TOCTOU',
        'ACTIVE RUNTIME BEHAVIOR = 0 CHANGE', '**P10-07D PASS.**',
    ):
        assert phrase in model
    assert 'P10-D5A-3 Episode Onboarding | **IMPLEMENTED / CLOSED**' \
        in backlog
    assert 'P10-D5A and P10-D3D-1B/C are `IMPLEMENTED / CLOSED`' in backlog
    assert '| P10-D3D-1B | immutable queue identity extension' in backlog
    assert '| P10-D3D-1B | immutable queue identity extension and four-tuple legacy drop parser | IMPLEMENTED / CLOSED |' in backlog
    assert '| P10-D3D-1C | worker V1/V2 validation and mutation claim before cancel/create | IMPLEMENTED / CLOSED |' in backlog
    assert 'P10-D3D-1C | worker V1/V2 validation' in backlog
    assert 'P10-D3D-1D | episode/generation/revision' in backlog
    assert '| P10-D3D-1D | episode/generation/revision conditional `algo_sl_id` writeback | BLOCKED_BY_R1_CANONICAL_PROJECTION_AND_D2A |' in backlog
