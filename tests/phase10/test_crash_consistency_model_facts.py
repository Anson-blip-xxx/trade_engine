"""P10-05 guards for current crash-consistency architecture facts."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / 'docs/v2/P10_D3_CRASH_CONSISTENCY_MODEL.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'


def _function(source, name, next_name):
    return source.split(f'def {name}', 1)[1].split(f'def {next_name}', 1)[0]


def _method(module_path, class_name, method_name):
    tree = ast.parse((ROOT / module_path).read_text())
    cls = next(node for node in tree.body
               if isinstance(node, ast.ClassDef) and node.name == class_name)
    return next(node for node in cls.body
                if isinstance(node, ast.FunctionDef) and node.name == method_name)


def test_dormant_operation_journal_foundation_has_no_runtime_or_outbox():
    schema = (ROOT / 'db/postgres_schema.sql').read_text().lower()
    operation_schema = (
        ROOT / 'db/postgres_operation_journal_schema.sql').read_text().lower()
    recorder = (ROOT / 'journal/recorder.py').read_text()
    assert 'operation_journal' not in schema
    assert 'transactional_outbox' not in schema
    assert 'create table if not exists trade_operations' in operation_schema
    assert not (ROOT / 'operation_journal/postgres.py').exists()
    assert 'class NullJournalRecorder' in recorder
    assert '_default_recorder: JournalRecorder = NullJournalRecorder()' in recorder


def test_s6_s8_startup_has_no_incomplete_operation_scan_or_replay():
    for path in ('strategies/S6.py', 'strategies/S8.py'):
        source = (ROOT / path).read_text()
        main = source.split('def main():', 1)[1]
        assert main.index('load_state(NAME)') < main.index(
            'reconcile_positions(NAME, state)')
        assert main.index('reconcile_positions(NAME, state)') < main.index(
            'while True:')
        for token in ('operation_id', 'nonterminal', 'recover_operations',
                      'replay_operations', 'reconcile_all('):
            assert token not in main


def test_strategy_reconcile_is_a_noop_not_workflow_recovery():
    source = (ROOT / 'strategies/shared_executor.py').read_text()
    body = _function(source, 'reconcile_positions(', '_refresh_positions(')
    assert 'return state' in body
    for token in ('operation_id', 'request_id', 'journal', 'replay',
                  'protection', 'UNKNOWN'):
        assert token not in body


def test_order_intent_has_no_request_operation_or_client_identity():
    source = (ROOT / 'execution/core.py').read_text()
    intent = source.split('class OrderIntent:', 1)[1].split(
        'def se_open_intent(', 1)[0]
    for token in ('request_id', 'operation_id', 'position_episode_id',
                  'newClientOrderId', 'client_order_id'):
        assert token not in intent
    assert 'symbol: str' in intent and 'quantity: float' in intent


def test_position_projection_has_no_revision_cas_or_ack():
    method = _method('position_state/service.py', 'PositionStateService', 'save')
    assert [arg.arg for arg in method.args.args] == ['self', 'positions']
    body = ast.unparse(method)
    for token in ('expected_version', 'revision', 'compare_and_swap',
                  'owner_token', 'operation_id'):
        assert token not in body
    assert 'except Exception:\n        pass' in body


def test_protection_handoff_is_process_memory_without_replay():
    pm = (ROOT / 'shared/position_manager.py').read_text()
    enqueue = _function(pm, '_algo_enqueue(', '_algo_place_sl_inner(')
    worker = (ROOT / 'position_runtime/runtime.py').read_text()
    loop = _function(worker, 'algo_worker_loop(', 'start_algo_worker(')
    assert '_ALGO_QUEUE = []' in pm
    assert '_ALGO_QUEUE.append(task)' in enqueue
    assert 'queue.pop(0)' in loop and 'queue.append' not in loop
    for token in ('operation_id', 'position_episode_id',
                  'attempt_count'):
        assert token not in enqueue
        assert token not in loop


def test_legacy_lifecycle_results_collapse_distinct_outcomes():
    executor_tree = ast.parse(
        (ROOT / 'strategies/shared_executor.py').read_text())
    active_open = next(node for node in executor_tree.body
                       if isinstance(node, ast.FunctionDef)
                       and node.name == 'open_position')
    close = _method('position_lifecycle/service.py',
                    'PositionLifecycleService', 'close')
    partial = _method('position_lifecycle/service.py',
                      'PositionLifecycleService', 'partial_close')
    assert isinstance(active_open.returns, ast.Name)
    assert active_open.returns.id == 'bool'
    assert isinstance(close.returns, ast.Name) and close.returns.id == 'bool'
    assert partial.returns is None
    assert any(isinstance(node, ast.Return) and node.value is None
               for node in ast.walk(partial))


def test_active_open_submits_before_any_durable_lifecycle_intent():
    source = (ROOT / 'strategies/shared_executor.py').read_text()
    body = _function(source, 'open_position(', '_round_qty(')
    submit = '_execution_service().execute_order(intent)'
    assert body.index(submit) < body.index('_update_pos_cache(')
    assert body.index(submit) < body.index('_pg_record_event(')
    for token in ('operation_id', 'request_id', 'INTENT_DURABLE',
                  'newClientOrderId'):
        assert token not in body


def test_close_coordination_is_symbol_scoped_not_operation_owned():
    reconcile = (ROOT / 'position_reconcile/service.py').read_text()
    ghost = _function(reconcile, 'ghost_cleanup(', 'ghost_cleanup_one(')
    state = (ROOT / 'position_state/service.py').read_text()
    marker = _function(state, 'mark_closed(', 'was_closed_recently(')
    assert "f'pm:ghost_close:{sym}'" in ghost
    assert "self.MARKER_PREFIX + symbol" in marker
    for token in ('operation_id', 'position_episode_id',
                  'owner_token'):
        assert token not in ghost
        assert token not in marker


def test_ghost_cleanup_removes_projection_before_record_and_marker():
    source = (ROOT / 'position_reconcile/service.py').read_text()
    body = _function(source, 'ghost_cleanup_one(', 'try_record_ghost_trade(')
    assert body.index('positions.pop(sym, None)') < body.index('record_trade(')
    assert body.index('record_trade(') < body.index('self.state.mc(sym)')


def test_reconcile_all_silently_removes_without_finalization_identity():
    source = (ROOT / 'position_reconcile/service.py').read_text()
    body = _function(source, 'reconcile_all(', 'notify_external_position(')
    assert 'positions.pop(sym, None)' in body
    assert 'self.state.save(positions)' in body
    for token in ('record_trade(', 'self.state.mc(', 'operation_id',
                  'position_episode_id', 'generation'):
        assert token not in body


def test_full_close_submission_precedes_required_accounting_and_removal():
    source = (ROOT / 'position_lifecycle/service.py').read_text()
    body = _function(source, 'close(', 'partial_close(')
    submit = 'self.execution.exec_fn().execute_order('
    assert body.index(submit) < body.index("'event_type': 'CLOSE_ORDER_FILLED'")
    assert body.index(submit) < body.rindex('record_trade(')
    assert body.rindex('record_trade(') < body.rindex(
        'positions.pop(symbol, None)')
    assert 'operation_id' not in body and 'UNKNOWN' not in body


def test_partial_close_has_no_durable_fill_or_operation_identity():
    source = (ROOT / 'position_lifecycle/service.py').read_text()
    body = _function(source, 'partial_close(', '__missing_next_function__(')
    assert 'execute_order(' in body and 'self.state.save(positions)' in body
    for token in ('operation_id', 'fill_id', 'orderId', 'record_trade(',
                  'self.action.pg(', 'UNKNOWN'):
        assert token not in body


def test_d3_document_and_backlog_record_design_without_readiness():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    for phrase in (
        '`OPEN`', '`CLOSE_FULL`', '`CLOSE_PARTIAL`',
        '`PROTECTION_CREATE`', '`PROTECTION_REPLACE`',
        '`GHOST_FINALIZE`', '`EXTERNAL_RECONCILE`',
        '`INTENT_DURABLE`', '`SUBMITTING`', '`UNKNOWN`',
        'operation_id != request_id != position_episode_id',
        'LOCK != RETRY IDEMPOTENCY', 'O1', 'O8', 'C1', 'C7',
        'SUPERSEDED_BY_D3_DESIGN', '**P10-05 PASS.**',
    ):
        assert phrase in model
    assert 'P10-D3 Crash Consistency Model | **DESIGN AUDITED**' in backlog
    assert '| P10-D3A |' in backlog and '| P10-D3E |' in backlog
    assert '| P10-D3A-D3E | none ready for production implementation |' \
        in backlog
