"""P10-01 guards for current open idempotency architecture facts."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _function(source, name, next_name):
    return source.split(f'def {name}', 1)[1].split(f'def {next_name}', 1)[0]


def test_s6_s8_active_open_wiring_uses_shared_executor():
    for strategy in ('S6.py', 'S8.py'):
        source = (ROOT / 'strategies' / strategy).read_text()
        imports = source.split('from shared_executor import (', 1)[1].split(
            ')', 1)[0]
        assert 'open_position' in imports
        assert 'shared.position_manager' not in source


def test_active_open_signature_and_payload_have_no_request_identity():
    executor = (ROOT / 'strategies/shared_executor.py').read_text()
    core = (ROOT / 'execution/core.py').read_text()
    signature = executor.split('def open_position(', 1)[1].split('):', 1)[0]
    params = core.split('class OrderIntent:', 1)[1].split(
        'def is_rejected', 1)[0]
    for token in ('request_id', 'idempotency', 'newClientOrderId',
                  'clientOrderId'):
        assert token not in signature
        assert token not in params


def test_active_position_id_is_generated_after_exchange_submission():
    source = (ROOT / 'strategies/shared_executor.py').read_text()
    open_body = _function(source, 'open_position(', '_round_qty(')
    assert open_body.index('execute_order(intent)') < \
        open_body.index('_update_pos_cache(')
    update = _function(source, '_update_pos_cache(', '_get_positions(')
    assert "position_id = f'{name}:{symbol}:{entry:.12g}:{now:.6f}'" in update


def test_active_open_has_no_durable_reservation_or_open_lock():
    source = (ROOT / 'strategies/shared_executor.py').read_text()
    open_body = _function(source, 'open_position(', '_round_qty(')
    before_order = open_body.split('execute_order(intent)', 1)[0]
    assert '_lock_acquire' not in open_body
    assert 'pm:open:' not in open_body
    assert "_rset('pm:positions'" not in before_order
    assert 'RESERVED' not in open_body and 'SUBMITTED' not in open_body


def test_execution_service_submits_once_without_retry_or_query():
    source = (ROOT / 'execution/service.py').read_text()
    method = next(node for node in ast.walk(ast.parse(source))
                  if isinstance(node, ast.FunctionDef)
                  and node.name == 'execute_order')
    calls = [node for node in ast.walk(method) if isinstance(node, ast.Call)]
    attrs = [call.func.attr for call in calls
             if isinstance(call.func, ast.Attribute)]
    assert attrs.count('place_order') == 1
    assert not any(isinstance(node, (ast.For, ast.While))
                   for node in ast.walk(method))
    assert not any(name.startswith(('retry', 'query')) for name in attrs)


def test_t1a_is_a_symbol_local_precheck_in_legacy_path_only():
    source = (ROOT / 'position_lifecycle/service.py').read_text()
    body = _function(source, 'open_position(', 'close_position(')
    assert body.index('positions = self.state.load()') < \
        body.index('if symbol in positions:') < \
        body.index("fapi_post('/fapi/v1/leverage'") < \
        body.index("fapi_post('/fapi/v1/order'")
    assert 'request_id' not in body
    assert 'lock' not in body.lower()


def test_no_restart_replay_for_incomplete_open_operations():
    executor = (ROOT / 'strategies/shared_executor.py').read_text()
    reconcile = _function(executor, 'reconcile_positions(', '_refresh_positions(')
    pm = (ROOT / 'shared/position_manager.py').read_text()
    assert 'return state' in reconcile
    assert 'request' not in reconcile.lower()
    assert 'order' not in reconcile.lower()
    assert '_ALGO_QUEUE = []' in pm


def test_d1_document_and_backlog_record_blocked_readiness():
    model = (ROOT / 'docs/v2/P10_D1_OPEN_IDEMPOTENCY_MODEL.md').read_text()
    backlog = (ROOT / 'docs/v2/PHASE10_BACKLOG.md').read_text()
    assert 'LOCK != RETRY IDEMPOTENCY' in model
    assert 'BLOCKED_BY_PRODUCT_DECISION' in model
    assert 'P10-D1 Open Idempotency Model | **DESIGN AUDITED**' in backlog
