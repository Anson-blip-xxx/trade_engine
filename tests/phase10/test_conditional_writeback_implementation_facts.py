"""R3 architecture guards for V3 conditional alias writeback."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / 'docs/v2/P10_R3_CONDITIONAL_ALIAS_WRITEBACK_IMPLEMENTATION.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'


def test_v3_task_carries_both_required_revisions():
    source = (ROOT / 'position_protection/task.py').read_text()
    assert 'class ConditionalWritebackProtectionTask(AlgoProtectionTask):' \
        in source
    assert 'desired_revision: int' in source
    assert 'projection_revision: int' in source


def test_writeback_lua_checks_all_fences_before_single_set():
    source = (ROOT / 'position_protection/writeback_redis.py').read_text()
    for token in (
        "authority['revision']", "claim['owner_token']",
        "claim['fencing_token']", "desired[\"episode_id\"]",
        "desired[\"slot_generation\"]",
        "desired[\"protection_generation\"]",
        "desired[\"revision\"]", "projection[\"state_revision\"]",
        "redis.call('SET', KEYS[3], updated)",
    ):
        assert token in source
    assert source.count("redis.call('SET'") == 1


def test_fenced_worker_explicitly_disables_legacy_writeback():
    source = (ROOT / 'shared/position_manager.py').read_text()
    execute = source.split('def _algo_execute_fenced_task(', 1)[1].split(
        'def _algo_enqueue(', 1)[0]
    assert 'ConditionalWritebackProtectionTask' in execute
    assert 'RedisConditionalAliasWritebackAdapter' in execute
    assert 'legacy_writeback=False' in execute
    assert 'desired_revision' in execute
    assert 'projection_revision' in execute


def test_ack_does_not_claim_active_or_protected():
    source = (ROOT / 'position_protection/writeback_redis.py').read_text()
    assert 'desired["status"] ~= "SUBMITTING"' in source
    assert "desired['status'] = 'ACTIVE'" not in source
    model = MODEL.read_text()
    assert 'does not set desired state to `ACTIVE`' in model
    assert "does not claim that an" in model
    assert "ACK proves `PROTECTED`" in model


def test_no_production_v3_producer_yet():
    callers = []
    for path in ROOT.rglob('*.py'):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ('tests', 'position_protection'):
            continue
        source = path.read_text()
        if 'desired_revision=' in source or 'projection_revision=' in source:
            callers.append(str(relative))
    assert callers == ['shared/position_manager.py']


def test_doc_and_backlog_close_r3_only():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    assert '**P10 R3 PASS.**' in model
    assert 'R3 / P10-D3D-1D Conditional Writeback | **IMPLEMENTED / CLOSED**' \
        in backlog
    assert 'R4 Active Producer Wiring | READY_AFTER_R3' in backlog
