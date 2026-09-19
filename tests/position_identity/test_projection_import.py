"""R1 import boundary and response hardening tests."""
import json
import os
import subprocess
import sys
from pathlib import Path

from position_identity.projection import ProjectionWriteCode
from position_identity.projection_redis import RedisLivePositionProjectionAdapter
from position_identity.slot import ExchangePositionKey

ROOT = Path(__file__).resolve().parents[2]


def test_adapter_import_is_io_free_in_clean_subprocess():
    code = (
        'import json, sys; import position_identity.projection_redis; '
        'blocked=("redis", "requests", "shared.position_manager", '
        '"position_runtime.runtime", "strategies.shared_executor"); '
        'print(json.dumps([name for name in blocked if name in sys.modules]))'
    )
    env = dict(os.environ)
    env['PYTHONPATH'] = str(ROOT)
    result = subprocess.run(
        [sys.executable, '-c', code], cwd=ROOT, env=env,
        check=True, capture_output=True, text=True,
    )
    assert json.loads(result.stdout) == []
    assert result.stderr == ''


def test_invalid_or_unknown_redis_responses_are_unknown_not_success():
    key = ExchangePositionKey.one_way(
        account_principal_id='test', environment='SANDBOX', symbol='ETHUSDT')
    for response in (None, [], ['APPLIED'], ['ALIEN', ''], [b'\xff', b'']):
        result = RedisLivePositionProjectionAdapter._parse_write(response, key)
        assert result.code is ProjectionWriteCode.UNKNOWN
        assert not result.applied


def test_storage_key_is_deterministic_dedicated_and_slot_scoped():
    first = ExchangePositionKey.one_way(
        account_principal_id='a', environment='SANDBOX', symbol='BTCUSDT')
    other = ExchangePositionKey.one_way(
        account_principal_id='a', environment='SANDBOX', symbol='ETHUSDT')
    adapter = RedisLivePositionProjectionAdapter(
        redis_get=lambda _key: None, redis_eval=lambda *_args: None)
    assert adapter._storage_key(first) == adapter._storage_key(first)
    assert adapter._storage_key(first) != adapter._storage_key(other)
    assert adapter._storage_key(first).startswith('pm:position-projection:v1:')
    assert 'pm:positions' not in adapter._storage_key(first)
