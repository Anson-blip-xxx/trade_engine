"""P10-D3D-1B architecture guards for immutable queue identity."""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / 'docs/v2/P10_D3D1B_QUEUE_IDENTITY_IMPLEMENTATION.md'
BACKLOG = ROOT / 'docs/v2/PHASE10_BACKLOG.md'


def _function(source, name, next_name):
    return source.split(f'def {name}', 1)[1].split(f'def {next_name}', 1)[0]


def test_task_is_frozen_leaf_with_complete_identity():
    source = (ROOT / 'position_protection/task.py').read_text()
    tree = ast.parse(source)
    task = next(node for node in tree.body
                if isinstance(node, ast.ClassDef)
                and node.name == 'AlgoProtectionTask')
    decorator = next(node for node in task.decorator_list
                     if isinstance(node, ast.Call)
                     and getattr(node.func, 'id', '') == 'dataclass')
    assert any(keyword.arg == 'frozen'
               and isinstance(keyword.value, ast.Constant)
               and keyword.value.value is True
               for keyword in decorator.keywords)
    fields = {node.target.id for node in task.body
              if isinstance(node, ast.AnnAssign)
              and isinstance(node.target, ast.Name)}
    assert fields == {
        'symbol', 'side', 'trigger_price', 'qty',
        'exchange_position_key', 'episode_id', 'slot_generation',
        'protection_generation',
    }


def test_enqueue_never_derives_partial_identity():
    source = (ROOT / 'shared/position_manager.py').read_text()
    enqueue = _function(source, '_algo_enqueue(', '_algo_place_sl_inner(')
    assert 'AlgoProtectionTask(' in enqueue
    assert "raise ValueError('queue identity fields must all be supplied')" \
        in enqueue
    for forbidden in ('_load(', '_rget(', 'position_id', 'uuid.'):
        assert forbidden not in enqueue


def test_worker_drops_legacy_before_place_without_authority_lookup():
    source = (ROOT / 'position_runtime/runtime.py').read_text()
    worker = _function(source, 'algo_worker_loop(', 'start_algo_worker(')
    assert worker.index('classify_queue_task(task)') < worker.index('place_fn(')
    assert 'QueueTaskClassification.FENCED' in worker
    assert 'reason={parsed.classification.value}' in worker
    for forbidden in (
        'get_slot_authority', 'RedisSlotAuthorityAdapter',
        'can_mutate_async', 'compare_and_transition',
    ):
        assert forbidden not in worker


def test_no_durable_queue_or_position_schema_wiring():
    pm = (ROOT / 'shared/position_manager.py').read_text()
    executor = (ROOT / 'strategies/shared_executor.py').read_text()
    cache = _function(executor, '_update_pos_cache(', '_get_positions(')
    assert '_ALGO_QUEUE = []' in pm
    for token in ('episode_id', 'slot_generation', 'protection_generation'):
        assert token not in cache


def test_model_records_scope_and_next_ticket():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    for phrase in (
        'LEGACY_UNFENCED', 'drop-only', 'must not be deployed by itself',
        'D3D-1C', 'D3D-1D', '**P10-D3D-1B PASS.**',
    ):
        assert phrase in model
    assert '| P10-D3D-1B | immutable queue identity extension' in backlog
