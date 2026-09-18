"""P10-04 guards for current persistence and side-effect architecture facts."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / 'docs/v2/P10_D4_PERSISTENCE_FAILURE_POLICY.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'


def _function(source, name, next_name):
    return source.split(f'def {name}', 1)[1].split(f'def {next_name}', 1)[0]


def test_pg_event_helper_can_return_false_or_propagate_preparation_error():
    source = (ROOT / 'shared/postgres_client.py').read_text()
    body = _function(source, 'record_trade_event(', '__missing_next_function__(')
    assert 'if not enabled()' in body and 'return False' in body
    assert body.index('params = dict(data)') < body.index('try:')
    assert "json.dumps(params.get('payload', {}), default=str)" in body
    assert 'except Exception:' in body


def test_active_open_ignores_pg_event_boolean_return():
    source = (ROOT / 'strategies/shared_executor.py').read_text()
    body = _function(source, 'open_position(', '_round_qty(')
    tree = ast.parse('def open_position(' + body)
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == '_pg_record_event'
    ]
    assert len(calls) == 1
    assert isinstance(next(node for node in ast.walk(tree)
                           if calls[0] in ast.iter_child_nodes(node)), ast.Expr)


def test_external_notification_ignores_pg_boolean_after_seen_state():
    source = (ROOT / 'position_reconcile/service.py').read_text()
    body = _function(source, 'notify_external_position(',
                     'migrate_existing_positions(')
    assert body.index('self.coordination.rdset(key') < \
        body.index('self.notification.pg({')
    assert body.index('self.coordination.rdset(pending_key, {})') < \
        body.index('self.notification.pg({')
    assert '= self.notification.pg(' not in body


def test_pm_positions_is_unacknowledged_operational_snapshot_state():
    source = (ROOT / 'execution/adapters/position_state.py').read_text()
    assert "PM_POSITIONS_KEY = 'pm:positions'" in source
    save = _function(source, 'save_positions(', '__missing_next_function__(')
    assert 'self._redis_set(PM_POSITIONS_KEY, positions)' in save
    assert 'except Exception:' in save and 'pass' in save
    assert 'return' not in save


def test_file_fallback_write_is_after_redis_and_has_no_ack():
    source = (ROOT / 'shared/redis_store.py').read_text()
    set_body = _function(source, 'set(', 'delete(')
    assert set_body.index('r.set(key, encoded)') < \
        set_body.index('_write_file(key, data)')
    assert 'return' not in set_body
    get_file = _function(source, '_get_file(', '_read_file(')
    assert 'if fp.exists() else None' in get_file


def test_notification_pending_and_seen_are_not_position_state():
    source = (ROOT / 'position_reconcile/service.py').read_text()
    body = _function(source, 'notify_external_position(',
                     'migrate_existing_positions(')
    assert "f'alert:external_position:{symbol}'" in body
    assert "f'alert:external_position:pending:{symbol}'" in body
    assert "'pm:positions'" not in body
    assert 'self.state.save' not in body


def test_clickhouse_has_no_lifecycle_recovery_reader():
    for path in (
        'position_state/service.py',
        'position_reconcile/service.py',
        'position_lifecycle/service.py',
        'position_protection/service.py',
    ):
        source = (ROOT / path).read_text().lower()
        assert 'clickhouse' not in source
        assert 'trade_history' not in source
        assert 'trade_analysis' not in source


def test_clickhouse_query_failure_is_fail_open_for_analysis_gate():
    client = (ROOT / 'shared/clickhouse_client.py').read_text()
    query = _function(client, 'query(', 'query_column(')
    executor = (ROOT / 'strategies/shared_executor.py').read_text()
    analysis = _function(executor, '_analysis_allows_open(',
                         '_record_analysis_decision(')
    assert 'except Exception:' in query and 'return []' in query
    assert "if trades < _ANALYSIS_FILTER_MIN_TRADES" in analysis
    assert "return True, '', 1.0" in analysis


def test_pg_episode_ack_is_discarded_before_ch_fanout():
    recorder = (ROOT / 'shared/trade_recorder.py').read_text()
    shim = recorder.split('class _PortShim:', 1)[1].split(
        'from position_ledger.service', 1)[0]
    ledger = (ROOT / 'position_ledger/service.py').read_text()
    settle = _function(ledger, 'settle(', '_cycle_pnl(')
    assert '_pg_upsert_trade(data)' in shim
    assert 'return _pg_upsert_trade(data)' not in shim
    assert settle.index('self._ledger_port.upsert_trade_episode({') < \
        settle.index("self._ch_insert('default.trade_history', row)")


def test_no_durable_operation_journal_or_transactional_outbox_schema():
    schema = (ROOT / 'db/postgres_schema.sql').read_text().lower()
    recorder = (ROOT / 'journal/recorder.py').read_text()
    assert 'operation_journal' not in schema
    assert 'outbox' not in schema
    assert 'class NullJournalRecorder' in recorder
    assert '_default_recorder: JournalRecorder = NullJournalRecorder()' in recorder


def test_pg_transaction_does_not_span_other_sinks():
    source = (ROOT / 'shared/postgres_client.py').read_text().lower()
    assert 'conn.commit()' in source and 'conn.rollback()' in source
    writes = _function(source, 'upsert_trade_episode(', 'record_trade_event(')
    writes += _function(source, 'record_trade_event(',
                        '__missing_next_function__(')
    for token in ('binance', 'redis', 'clickhouse', 'telegram', 'algoorder'):
        assert token not in writes


def test_d4_document_and_backlog_record_readiness():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    for phrase in (
        '`REQUIRED`', '`BEST_EFFORT`', '`RETRYABLE`', '`UNKNOWN`',
        '`DERIVED`', '`CACHE_ONLY`',
        'P10-D3 | `READY_FOR_DESIGN`',
        '**P10-04 PASS.**',
    ):
        assert phrase in model
    assert 'P10-D4 Persistence Failure Policy | **DESIGN AUDITED**' in backlog
    assert '| PMB-26C2 | B Close/Reconcile Durability | BLOCKED_BY_PRODUCT_DECISION' \
        in backlog
