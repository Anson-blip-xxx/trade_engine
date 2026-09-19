"""P10-D3D-1C architecture guards for V1/V2 mutation permission."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / 'docs/v2/P10_D3D1C_WORKER_PREFLIGHT_IMPLEMENTATION.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'


def _function(source, name, next_name):
    return source.split(f'def {name}', 1)[1].split(f'def {next_name}', 1)[0]


def test_v1_precedes_cancel_and_v2_precedes_create():
    pm = (ROOT / 'shared/position_manager.py').read_text()
    execute = _function(pm, '_algo_execute_fenced_task(', '_algo_enqueue(')
    place = _function(pm, '_algo_place_sl_inner(', '_algo_cancel(')
    assert execute.index('fence.acquire(task)') < execute.index(
        '_algo_place_sl_inner(')
    assert place.index('_cancel_all_algo(symbol)') < place.index(
        'before_create()') < place.index(
        "_light_fapi_post('/fapi/v1/algoOrder'")
    assert 'fence.validate(claim)' in execute
    assert 'fence.release(claim)' in execute


def test_claim_scripts_bind_authority_owner_and_protection_generation():
    source = (ROOT / 'position_protection/claim_redis.py').read_text()
    for token in (
        "authority['revision']", "authority['status'] ~= 'ACTIVE'",
        "authority['episode_id']", "authority['slot_generation']",
        "claim['protection_generation']", "claim['owner_token']",
        "claim['fencing_token']", "redis.call('INCR', KEYS[3])",
        "redis.call('PEXPIRE', KEYS[2], ARGV[7])",
    ):
        assert token in source


def test_authority_redis_seams_have_no_file_fallback():
    source = (ROOT / 'shared/redis_store.py').read_text()
    strict = _function(source, 'strict_get(', 'migrate_all(')
    assert '_conn().get(key)' in strict
    assert '_conn().eval(script, numkeys, *keys_and_args)' in strict
    for token in ('_read_file(', '_write_file(', 'KEY_MAP'):
        assert token not in strict


def test_v3_writeback_and_durable_desired_generation_remain_deferred():
    pm = (ROOT / 'shared/position_manager.py').read_text()
    place = _function(pm, '_algo_place_sl_inner(', '_algo_cancel(')
    executor = (ROOT / 'strategies/shared_executor.py').read_text()
    cache = _function(executor, '_update_pos_cache(', '_get_positions(')
    assert "positions[symbol]['algo_sl_id'] = result['algoId']" in place
    for token in ('expected_episode', 'expected_revision', 'compare_and_swap'):
        assert token not in place
    assert 'protection_generation' not in cache


def test_active_producers_remain_legacy_and_ticket_is_not_deployable():
    executor = (ROOT / 'strategies/shared_executor.py').read_text()
    active_open = _function(executor, 'open_position(', '_round_qty(')
    lifecycle = (ROOT / 'position_lifecycle/service.py').read_text()
    legacy_open = _function(lifecycle, 'open_position(', 'close_position(')
    for body in (active_open, legacy_open):
        assert 'exchange_position_key=' not in body
        assert 'protection_generation=' not in body
    model = MODEL.read_text()
    assert 'must not be deployed by itself' in model


def test_model_closes_d3d1c_and_advances_d3d1d():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    for phrase in (
        'V1 before any protection cancel',
        'V2 after cancel/query and immediately before create',
        'pm:protection-claim:v1:', 'pm:protection-fence:v1:',
        'D3D-1D', 'D3D-2', '**P10-D3D-1C PASS.**',
    ):
        assert phrase in model
    assert '| P10-D3D-1C | worker V1/V2 validation and mutation claim before cancel/create | IMPLEMENTED / CLOSED |' in backlog
    assert '| P10-D3D-1D | episode/generation/revision conditional `algo_sl_id` writeback | BLOCKED_BY_R1_CANONICAL_PROJECTION_AND_D2A |' in backlog
