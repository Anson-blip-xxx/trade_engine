"""P10-03 guards for current position identity and marker architecture facts."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / 'docs/v2/P10_D5_POSITION_IDENTITY_MARKER_AUTHORITY.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'


def _function(source, name, next_name):
    return source.split(f'def {name}', 1)[1].split(f'def {next_name}', 1)[0]


def test_active_position_id_is_post_order_and_timestamp_based():
    source = (ROOT / 'strategies/shared_executor.py').read_text()
    open_body = _function(source, 'open_position(', '_round_qty(')
    update = _function(source, '_update_pos_cache(', '_get_positions(')
    assert open_body.index('execute_order(intent)') < \
        open_body.index('_update_pos_cache(')
    assert 'now = time.time()' in update
    assert "position_id = f'{name}:{symbol}:{entry:.12g}:{now:.6f}'" \
        in update


def test_current_position_state_and_exchange_snapshot_are_symbol_keyed():
    executor = (ROOT / 'strategies/shared_executor.py').read_text()
    update = _function(executor, '_update_pos_cache(', '_get_positions(')
    state = (ROOT / 'position_state/service.py').read_text()
    parser = _function(state, 'parse_position_risk(', '__missing_next_function__(')
    assert '_POS_CACHE[symbol]' in update
    assert "meta[symbol] = dict(_POS_CACHE[symbol])" in update
    assert "out[p['symbol']]" in parser
    assert 'position_id' not in parser


def test_closed_marker_is_symbol_keyed_timestamp_only():
    source = (ROOT / 'position_state/service.py').read_text()
    marker = ''.join((
        _function(source, 'mark_closed(', 'was_closed_recently('),
        _function(source, 'was_closed_recently(', 'clear_closed('),
        _function(source, 'clear_closed(', 'load_assembly('),
    ))
    assert "MARKER_PREFIX = 'closed:'" in source
    assert 'self.MARKER_PREFIX + symbol' in marker
    assert "{'ts': time.time()}" in marker
    for token in ('position_id', 'episode_id', 'generation', 'system', 'side'):
        assert token not in marker


def test_closed_marker_write_has_no_ttl_or_expiry():
    source = (ROOT / 'position_state/service.py').read_text()
    mark = _function(source, 'mark_closed(', 'was_closed_recently(')
    tree = ast.parse('def mark_closed(' + mark)
    marker_set = next(
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == '_marker_set')
    assert len(marker_set.args) == 2
    assert not marker_set.keywords
    called_attrs = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not called_attrs.intersection({'expire', 'setex', 'pexpire'})


def test_marker_clear_is_unconditional_symbol_level_delete():
    source = (ROOT / 'position_state/service.py').read_text()
    clear = _function(source, 'clear_closed(', 'load_assembly(')
    signature = source.split('def clear_closed(', 1)[1].split(') ->', 1)[0]
    assert signature.strip() == 'self, symbol: str'
    assert 'self._marker_delete(self.MARKER_PREFIX + symbol)' in clear
    for token in ('expected', 'episode', 'generation', 'compare'):
        assert token not in clear.lower()


def test_marker_malformed_and_stale_values_fail_open_without_cleanup():
    source = (ROOT / 'position_state/service.py').read_text()
    recent = _function(source, 'was_closed_recently(', 'clear_closed(')
    assert "if data and 'ts' in data" in recent
    assert "time.time() - data['ts'] < within_hours * 3600" in recent
    assert 'return False' in recent
    assert '_marker_delete' not in recent


def test_protection_task_has_no_episode_or_generation_identity():
    source = (ROOT / 'shared/position_manager.py').read_text()
    enqueue = _function(source, '_algo_enqueue(', '_algo_place_sl_inner(')
    assert '_ALGO_QUEUE.append((symbol, side, trigger_price, qty))' in enqueue
    for token in ('position_id', 'episode', 'generation', 'request_id'):
        assert token not in enqueue


def test_reconcile_exchange_snapshot_cannot_recover_episode_identity():
    source = (ROOT / 'position_reconcile/service.py').read_text()
    reconcile = _function(source, 'reconcile_all(', 'notify_external_position(')
    assert "real_positions[p['symbol']]" in reconcile
    assert "'amt': amt" in reconcile and "'side':" in reconcile
    for token in ('position_id', 'episode_id', 'orderId', 'clientOrderId',
                  'open_time'):
        assert token not in reconcile


def test_current_merge_fallback_generates_a_different_position_id_shape():
    source = (ROOT / 'shared/position_manager.py').read_text()
    merge = _function(source, '_merge_meta(', '_merge_meta_preserving_missing(')
    fallback = _function(source, '_position_id(', '_notify_external_position(')
    assert 'f"{system_name}:{sym}:{mp.get(\'open_time\', now):.6f}"' in merge
    assert "str(pos.get('position_id') or ':'.join([" in fallback
    assert "f\"{float(pos.get('open_time', 0)):.6f}\"" in fallback


def test_position_id_is_identity_critical_for_partial_and_pg_ledgers():
    recorder = (ROOT / 'shared/trade_recorder.py').read_text()
    schema = (ROOT / 'db/postgres_schema.sql').read_text()
    assert 'hashlib.sha1(position_id.encode()).hexdigest()' in recorder
    assert 'position_id TEXT PRIMARY KEY' in schema
    assert 'WHERE position_id = t.position_id' in schema


def test_existing_position_migration_has_no_schema_or_episode_version():
    source = (ROOT / 'position_reconcile/service.py').read_text()
    migrate = _function(source, 'migrate_existing_positions(',
                        '__missing_next_function__(')
    assert "pos['system']" in migrate
    assert "pos['side']" in migrate
    assert "pos['original_qty']" in migrate
    for token in ('schema_version', 'state_version', 'position_episode_id',
                  'protection_generation', 'migration_version'):
        assert token not in migrate


def test_d5_document_and_backlog_record_blocked_readiness():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    for phrase in (
        'request_id != position_episode_id',
        'EXCHANGE PHYSICAL EXPOSURE > RECENT CLOSED MARKER',
        'PMB-4A', 'PMB-4B', 'PMB-4C',
        '**BLOCKED_BY_PRODUCT_DECISION**',
        '**P10-03 PASS.**',
    ):
        assert phrase in model
    assert 'P10-D5 Position Identity And Marker Authority | **DESIGN AUDITED**' \
        in backlog
    assert '| POS-ID | C Identity/Marker Model | BLOCKED_BY_PRODUCT_DECISION' \
        in backlog
