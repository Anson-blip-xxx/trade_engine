"""P10-07 guards for current episode-authority readiness facts."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / 'docs/v2/P10_D5A_EPISODE_AUTHORITY_READINESS.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'


def _function(source, name, next_name):
    return source.split(f'def {name}', 1)[1].split(f'def {next_name}', 1)[0]


def test_current_auth_has_no_account_principal_abstraction():
    auth = (ROOT / 'position_market/auth.py').read_text()
    binance = (ROOT / 'shared/binance_api.py').read_text()
    assert 'def load_api_keys(' in auth
    assert 'return api_key, secret' in auth
    assert "API_KEY = CFG['BINANCE_API_KEY']" in binance
    assert "SECRET = CFG['BINANCE_API_SECRET']" in binance
    for token in ('account_principal_id', 'account_alias', 'subaccount_id',
                  'credential_profile'):
        assert token not in auth
        assert token not in binance


def test_current_environment_label_omits_sandbox_dimension():
    binance = (ROOT / 'shared/binance_api.py').read_text()
    get_env = _function(binance, 'get_env(', 'sign(')
    sandbox = (ROOT / 'scripts/sandbox.py').read_text()
    assert "return 'demo' if _IS_TESTNET else 'prod'" in get_env
    assert "os.environ.get('SANDBOX'" in sandbox
    assert 'SANDBOX' not in get_env


def test_active_state_lacks_provenance_and_episode_status():
    source = (ROOT / 'strategies/shared_executor.py').read_text()
    cache = _function(source, '_update_pos_cache(', '_get_positions(')
    assert "'position_id': position_id" in cache
    for token in ('identity_provenance', 'episode_status', 'schema_version',
                  'legacy_position_id'):
        assert token not in cache


def test_no_dedicated_slot_authority_key_exists():
    redis_store = (ROOT / 'shared/redis_store.py').read_text()
    adapter = (ROOT / 'execution/adapters/position_state.py').read_text()
    assert "'pm:positions'" in redis_store
    assert "PM_POSITIONS_KEY = 'pm:positions'" in adapter
    for token in ('pm:slot:', 'slot_authority', 'current_episode_id'):
        assert token not in redis_store
        assert token not in adapter


def test_no_durable_slot_generation_owner_exists():
    paths = (
        'shared/redis_store.py',
        'position_state/service.py',
        'execution/adapters/position_state.py',
        'position_reconcile/service.py',
    )
    for path in paths:
        source = (ROOT / path).read_text()
        assert 'slot_generation' not in source
        assert 'generation_high_water' not in source


def test_positions_snapshot_save_is_unacknowledged_overwrite():
    adapter = (ROOT / 'execution/adapters/position_state.py').read_text()
    save = _function(adapter, 'save_positions(', '__missing_next_function__(')
    assert 'self._redis_set(PM_POSITIONS_KEY, positions)' in save
    assert 'except Exception:' in save and 'pass' in save
    tree = ast.parse('def save_positions(' + save)
    returns = [node for node in ast.walk(tree) if isinstance(node, ast.Return)]
    assert returns == []
    for token in ('expected_revision', 'compare', 'cas', 'generation'):
        assert token not in save.lower()


def test_redis_has_lock_lua_but_no_generation_allocator_or_state_cas():
    source = (ROOT / 'shared/redis_store.py').read_text()
    acquire = _function(source, 'lock_acquire(', 'lock_renew(')
    renew = _function(source, 'lock_renew(', 'lock_release(')
    release = _function(source, 'lock_release(', '__missing_next_function__(')
    assert 'nx=True' in acquire and 'ex=ttl' in acquire
    assert 'r.eval(lua' in renew
    assert '_conn().eval(lua' in release
    for token in ('.incr(', '.incrby(', '.watch(', '.multi(', '.pipeline('):
        assert token not in source
    for token in ('compare_and_transition_slot', 'expected_revision',
                  'slot_generation'):
        assert token not in source


def test_legacy_active_migration_lacks_episode_authority():
    source = (ROOT / 'position_reconcile/service.py').read_text()
    migrate = _function(source, 'migrate_existing_positions(',
                        '__missing_next_function__(')
    assert "pos['system']" in migrate
    assert "pos['original_qty']" in migrate
    for token in ('episode_id', 'slot_generation', 'revision', 'provenance',
                  'quarantine', 'compare_and_swap'):
        assert token not in migrate.lower()


def test_reconstructed_merge_has_no_provenance_or_quarantine_gate():
    source = (ROOT / 'shared/position_manager.py').read_text()
    merge = _function(source, '_merge_meta(', '_merge_meta_preserving_missing(')
    save = _function(source, '_merge_and_save(', '_meta_filtered(')
    assert "system_name = mp.get('system'" in merge
    assert "'position_id': mp.get(" in merge
    assert '_save(merged)' in save
    for token in ('RECONSTRUCTED', 'identity_provenance', 'QUARANTINED',
                  'slot_generation', 'episode_id'):
        assert token not in merge


def test_current_close_does_not_require_episode_authority_before_submit():
    lifecycle = (ROOT / 'position_lifecycle/service.py').read_text()
    close = _function(lifecycle, 'close(', 'partial_close(')
    submit_window = close.split('requested_close_qty =', 1)[1].split(
        'result = self.execution.exec_fn().execute_order(', 1)[0]
    assert 'real_pos' in submit_window
    for token in ('episode_id', 'slot_generation', 'provenance',
                  'expected_revision', 'compare_and_swap'):
        assert token not in submit_window
    core = (ROOT / 'execution/core.py').read_text()
    intent = _function(core, 'close_intent(', 'partial_close_intent(')
    assert "positionSide='BOTH'" in intent
    assert "reduceOnly='true'" in intent
    assert 'episode' not in intent


def test_no_episode_allocation_cas_exists_in_state_or_reconcile():
    combined = ''.join(
        (ROOT / path).read_text()
        for path in (
            'position_state/service.py',
            'position_reconcile/service.py',
            'execution/adapters/position_state.py',
        )
    )
    for token in ('allocate_episode', 'adopt_episode',
                  'compare_and_transition_slot', 'create_if_absent',
                  'expected_slot_generation'):
        assert token not in combined


def test_account_endpoint_and_lock_uid_do_not_supply_account_identity():
    sandbox = (ROOT / 'scripts/sandbox.py').read_text()
    account = _function(sandbox, 'mock_get_account(', 'mock_get_open_orders(')
    assert "'walletBalance'" in account and "'canTrade': True" in account
    for token in ("'uid'", "'accountId'", "'subaccount'", "'principal'"):
        assert token not in account
    pm = (ROOT / 'shared/position_manager.py').read_text()
    assert 'lambda: uuid.uuid4().hex[:8]' in pm


def test_readiness_document_and_backlog_finalize_staged_readiness():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    for phrase in (
        'explicit configured non-secret logical principal alias',
        'CONTROLLED_ADOPTION', 'RECONSTRUCTED_QUARANTINED',
        'NATIVE', 'MIGRATED', 'RECONSTRUCTED',
        'dedicated Redis slot authority key/hash',
        'compare_and_transition_slot',
        'READY_FOR_IMPLEMENTATION', 'READY_AFTER_D5A_FOUNDATION',
        'P10-07B / D5A-1 Slot Namespace + Principal Resolver',
        '**P10-07 PASS.**',
    ):
        assert phrase in model
    assert 'P10-D5A Episode Authority Readiness | **DESIGN AUDITED**' \
        in backlog
    assert '| P10-D5A | normalized slot' in backlog
    assert '| P10-D3D-1 | protection queue episode fence' in backlog
    assert 'P10-07B / D5A-1' in backlog
