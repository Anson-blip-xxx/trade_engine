"""P10-00 guards for current reliability architecture facts."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
AUDIT = ROOT / 'docs/v2/PHASE10_RELIABILITY_AUDIT.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'

IMPORTED = {
    'T1-B', 'T5', 'PMB-23A', 'T12', 'PMB-26C1', 'PMB-26C2',
    'PMB-24', 'PMB-26B2', 'POS-ID', 'PMB-4',
}


def _backlog_ids(source):
    block = source.split('<!-- P10_BACKLOG_START -->', 1)[1].split(
        '<!-- P10_BACKLOG_END -->', 1)[0]
    result = set()
    for line in block.splitlines():
        if line.startswith('| ') and not line.startswith('|---'):
            ticket = line.split('|')[1].strip()
            if ticket != 'ID':
                result.add(ticket)
    return result


def test_backlog_is_imported_exactly_from_phase9():
    assert _backlog_ids(BACKLOG.read_text()) == IMPORTED


def test_open_order_has_no_preorder_idempotency_field():
    core = (ROOT / 'execution/core.py').read_text()
    open_intent = core.split('class OrderIntent:', 1)[1].split(
        'def is_rejected', 1)[0]
    assert 'newClientOrderId' not in open_intent
    assert 'clientOrderId' not in open_intent
    assert 'request_id' not in open_intent


def test_active_open_has_no_open_scoped_lock():
    source = (ROOT / 'strategies/shared_executor.py').read_text()
    open_body = source.split('def open_position(', 1)[1].split(
        'def _round_qty', 1)[0]
    assert 'pm:open:' not in open_body
    assert '_lock_acquire' not in open_body
    assert 'newClientOrderId' not in open_body


def test_algo_queue_is_process_memory_and_sleep_is_after_attempt():
    pm = (ROOT / 'shared/position_manager.py').read_text()
    runtime = (ROOT / 'position_runtime/runtime.py').read_text()
    enqueue = pm.split('def _algo_enqueue', 1)[1].split(
        'def _algo_place_sl_inner', 1)[0]
    worker = runtime.split('def algo_worker_loop', 1)[1].split(
        'def start_algo_worker', 1)[0]
    assert '_ALGO_QUEUE = []' in pm
    assert '_ALGO_QUEUE.append' in enqueue
    assert '_rset' not in enqueue and 'postgres' not in enqueue
    assert worker.index('place_fn(') < worker.index('time.sleep(11)')
    assert 'queue.pop(0)' in worker and 'time.sleep(1)' in worker


def test_closed_marker_is_symbol_timestamp_without_ttl_or_position_id():
    source = (ROOT / 'position_state/service.py').read_text()
    marker = source.split('def mark_closed', 1)[1].split(
        'def load_assembly', 1)[0]
    assert "{'ts': time.time()}" in marker
    assert 'within_hours' in marker
    assert 'position_id' not in marker
    assert 'expire' not in marker.lower()
    assert 'setex' not in marker.lower()
    assert 'ex=' not in marker.lower()


def test_audit_contains_required_boundary_models_and_zero_diff():
    source = AUDIT.read_text()
    for heading in (
        'Source-Of-Truth Matrix', 'Commit Boundary Map', 'Crash Matrix',
        'Failure-Domain Matrix', 'Durability And Restart Matrix',
        'Distributed Assumptions', 'Dependency DAG',
        'Implementation Readiness',
    ):
        assert heading in source
    assert 'Production behavior remains unchanged' in source
    assert 'P10-00 PASS' in source
