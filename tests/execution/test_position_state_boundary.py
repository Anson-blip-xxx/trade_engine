"""P4-03-01-D2：Position State Redis boundary contract / parity tests。

覆盖（prompt 测试要求 1-15 + parity）：
1 Port contract（最小面）
2-4 adapter load/save（delete 不在：pm:positions 无删除 seam——closed marker 属
   Closed Marker 类，显式排除）
5-7 key/value 一致（'pm:positions' 逐字）+ TTL（无）
8 missing key 行为（→ {}）
9 Redis exception 行为（→ {} / 静默）
10 malformed value 行为（非 dict 顶层/值 → {}）
11 adapter 不创建 Redis client（callable 注入，源码无 redis import）
12-14 adapter 不依赖 PM / shared_executor / strategies
15 无真实 Redis 网络请求（fake helper 注入）
+ parity：adapter 行锁 == PM._load_meta / PM._save（同一 fake store）
+ pilot wiring 冻结：PM._save 经 boundary 一次调用
"""
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

from execution.adapters.position_state import PM_POSITIONS_KEY, RedisPositionStateAdapter
from execution.ports.position_state import PositionStatePort


class FakeRedis:
    """shared.redis_store.get/set 同行为 fake：记录调用；可注入异常。"""

    def __init__(self, get_exc=None, set_exc=None):
        self.store = {}
        self.get_calls = 0
        self.set_calls = []
        self.get_exc = get_exc
        self.set_exc = set_exc

    def get(self, key):
        self.get_calls += 1
        if self.get_exc is not None:
            raise self.get_exc
        return self.store.get(key)

    def set(self, key, data):
        self.set_calls.append((key, data))
        if self.set_exc is not None:
            raise self.set_exc
        self.store[key] = data


POS = {'AUSDT': {'entry': 2.0, 'qty': 10.0, 'side': 'SHORT', 'system': 'S8'}}


def make_adapter(fake=None):
    fake = fake or FakeRedis()
    return RedisPositionStateAdapter(fake.get, fake.set), fake


# ═══════════════════════════════════════════════════════════════
#  1. Port contract
# ═══════════════════════════════════════════════════════════════

class TestPortContract:
    def test_port_declares_load_save_only(self):
        """最小面：load_positions / save_positions（无 per-symbol 增删接口）。"""
        members = [name for name, _ in inspect.getmembers(
            PositionStatePort, predicate=inspect.isfunction)
            if not name.startswith('_')]
        assert members == ['load_positions', 'save_positions']

    def test_adapter_satisfies_port(self):
        adapter, _ = make_adapter()
        assert isinstance(adapter, PositionStatePort)

    def test_scope_frozen_single_key(self):
        """boundary 只覆盖 Position State 单 key：adapter key 常量唯一为 pm:positions，
        Port 无 delete/marker 方法（closed marker 属 Closed Marker 类，显式排除）。"""
        from execution.adapters.position_state import PM_POSITIONS_KEY as K
        assert K == 'pm:positions'
        members = [name for name, _ in inspect.getmembers(
            PositionStatePort, predicate=inspect.isfunction)
            if not name.startswith('_')]
        assert not any('delete' in m or 'close' in m or 'marker' in m
                       for m in members)


# ═══════════════════════════════════════════════════════════════
#  2-7. Adapter get/set + key/value/TTL
# ═══════════════════════════════════════════════════════════════

class TestAdapterOps:
    def test_load_roundtrip_value_unchanged(self):
        """序列化无关层验证：存什么读回什么（dict 原样，helper 内 JSON）。"""
        adapter, fake = make_adapter()
        adapter.save_positions(POS)
        assert adapter.load_positions() == POS

    def test_key_is_pm_positions_verbatim(self):
        assert PM_POSITIONS_KEY == 'pm:positions'
        adapter, fake = make_adapter()
        adapter.save_positions(POS)
        assert fake.set_calls == [('pm:positions', POS)]    # key 逐字一致
        adapter.load_positions()
        assert fake.get_calls == 1

    def test_nested_value_structures_preserved(self):
        """JSON 序列化前后等价（default=str 之外的类型原样经 helper 往返）。"""
        adapter, fake = make_adapter()
        data = {'XUSDT': {'tp_done': [5.0, 8.0], 'algo_sl_id': 123,
                          'nested': {'a': 1}, 'entry': 0.001}}
        adapter.save_positions(data)
        assert adapter.load_positions() == data

    def test_save_dataset_semantics_whole_snapshot(self):
        """save 是整量快照写入：后写覆盖前写（PM._save 语义）。"""
        adapter, fake = make_adapter()
        adapter.save_positions({'AUSDT': {'qty': 1}})
        adapter.save_positions({'BUSDT': {'qty': 2}})
        assert adapter.load_positions() == {'BUSDT': {'qty': 2}}

    def test_save_filters_in_load_only(self):
        """写入不做过滤（_save 原样）；读取时过滤非 dict 值（_load_meta 原样）。"""
        adapter, _ = make_adapter()
        dirty = {'AUSDT': {'qty': 1}, 'BAD': 'not-a-dict'}
        adapter.save_positions(dirty)                  # 原样存储
        assert adapter.load_positions() == {'AUSDT': {'qty': 1}}


# ═══════════════════════════════════════════════════════════════
#  8-10. missing / exception / malformed
# ═══════════════════════════════════════════════════════════════

class TestExceptionSemantics:
    def test_missing_key_returns_empty_dict(self):
        adapter, _ = make_adapter()
        assert adapter.load_positions() == {}

    def test_get_exception_returns_empty_dict_silent(self):
        """helper 异常 → {}（_load_meta 吞错语义原样，不改善）。"""
        adapter, _ = make_adapter(FakeRedis(get_exc=RuntimeError('conn lost')))
        assert adapter.load_positions() == {}

    def test_set_exception_swallowed_silent(self):
        """helper 写异常 → 静默（_save 吞错语义原样），无任何上抛。"""
        fake = FakeRedis(set_exc=RuntimeError('conn lost'))
        adapter, _ = make_adapter(fake)
        adapter.save_positions(POS)         # 不得 raise
        assert fake.store.get('pm:positions') is None   # 失败写无任何数据

    def test_non_dict_top_level_returns_empty(self):
        adapter, fake = make_adapter()
        fake.store['pm:positions'] = ['not', 'a', 'dict']
        assert adapter.load_positions() == {}


# ═══════════════════════════════════════════════════════════════
#  Parity：adapter 行为 == PM production seam（同一 fake store 双路径对拍）
# ═══════════════════════════════════════════════════════════════

@pytest.fixture
def pmparity(monkeypatch):
    """PM._load_meta/_save 与 adapter 绑定到同一个 FakeRedis，双路径对拍。"""
    from shared import position_manager as pm
    fake = FakeRedis()
    monkeypatch.setattr(pm, '_rget', fake.get)
    monkeypatch.setattr(pm, '_rset', fake.set)
    adapter = RedisPositionStateAdapter(fake.get, fake.set)
    return pm, adapter, fake


class TestParityWithProductionSeam:
    def test_save_parity_same_store_state(self, pmparity):
        pm, adapter, fake = pmparity
        adapter.save_positions(POS)
        assert fake.set_calls == [('pm:positions', POS)]
        pm._save(POS)
        assert fake.set_calls == [('pm:positions', POS),
                                  ('pm:positions', POS)]
        assert adapter.load_positions() == pm._load_meta() == POS

    def test_load_parity_malformed(self, pmparity):
        pm, adapter, fake = pmparity
        fake.store['pm:positions'] = {'A': {'q': 1}, 'B': 'bad', 'C': 3}
        assert pm._load_meta() == adapter.load_positions() == {'A': {'q': 1}}

    def test_load_parity_missing(self, pmparity):
        pm, adapter, _ = pmparity
        assert pm._load_meta() == adapter.load_positions() == {}

    def test_load_parity_helper_exception(self, pmparity, monkeypatch):
        pm, adapter, fake = pmparity
        def boom(key):
            raise RuntimeError('down')
        monkeypatch.setattr(pm, '_rget', boom)
        adapter2 = RedisPositionStateAdapter(boom, pm._rset)
        assert pm._load_meta() == adapter2.load_positions() == {}

    def test_save_parity_helper_exception(self, pmparity, monkeypatch):
        pm, adapter, fake = pmparity
        def boom(key, val):
            raise RuntimeError('down')
        monkeypatch.setattr(pm, '_rset', boom)
        adapter2 = RedisPositionStateAdapter(pm._rget, boom)
        pm._save(POS)                       # 静默
        adapter2.save_positions(POS)        # 静默
        assert fake.store == {}             # 两者均未写入


# ═══════════════════════════════════════════════════════════════
#  Pilot wiring 冻结：PM._save 经 boundary（一次调用，其余路径不动）
# ═══════════════════════════════════════════════════════════════

class TestPilotWiring:
    def test_save_routes_through_boundary(self, monkeypatch):
        """PM._save 恰好一次经 PositionStatePort（_rset 一次调用）。"""
        from shared import position_manager as pm
        fake = FakeRedis()
        monkeypatch.setattr(pm, '_rget', fake.get)
        monkeypatch.setattr(pm, '_rset', fake.set)
        pm._save(POS)
        assert fake.set_calls == [('pm:positions', POS)]     # key/value/TTL 逐字
        assert pm._load_meta() == POS

    def test_load_meta_now_wired_via_service(self, monkeypatch):
        """P7-02 read pilot：_load_meta 经 PositionStateService（仍零 IO 直连）。"""
        src = inspect.getsource(
            __import__('shared.position_manager', fromlist=['x'])._load_meta)
        assert '_rget' not in src                 # IO 留在 boundary 内
        assert '_exec_pos_state' not in src       # 走 StateService 门面
        assert '_state_service()' in src

    def test_other_redis_keys_not_wired(self):
        """closed marker 的 delete 仍直连 redis_store（OBS-5），不走 boundary；
        锁 / 告警的 Redis 访问不经 boundary（现状保留）。"""
        guard_src = inspect.getsource(
            __import__('position_state.adapters' if False else 's0.adapters',
                       fromlist=['x']))
        # OBS-5 直连 delete 语义：经 `_state_service()._direct_marker_delete`
        # 内 `from shared.redis_store import delete as _direct_delete` 注入保留。
        assert 'from shared.redis_store import delete as _direct_delete' in \
            __import__('shared.position_manager', fromlist=['x']).__dict__ \
            ['_state_service'].__doc__ + \
            inspect.getsource(
                __import__('shared.position_manager', fromlist=['x'])
                    ._state_service)


# ═══════════════════════════════════════════════════════════════
#  11-15：隔离与依赖边界
# ═══════════════════════════════════════════════════════════════

class TestDependencyBoundary:
    FORBIDDEN = ('redis', 'requests', 'binance', 'strategies',
                 'shared_executor', 'position_manager', 'shared.',
                 'psycopg', 'telegram', 'threading', 'queue', 'time',
                 'socket', 'os')

    @pytest.mark.parametrize('module', [
        'execution.ports.position_state', 'execution.adapters.position_state'])
    def test_source_has_no_forbidden_imports(self, module):
        src = inspect.getsource(__import__(module, fromlist=['x']))
        for mod in self.FORBIDDEN:
            assert not re.search(rf'^\s*(import|from)\s+{re.escape(mod)}',
                                 src, re.M), f'forbidden import: {mod}'

    def test_clean_import_pulls_no_io_modules(self):
        """子进程干净 import → 无 redis/requests/strategies/shared 模块。"""
        repo = Path(__file__).resolve().parents[2]
        code = ("import sys; "
                "import execution.ports.position_state, "
                "execution.adapters.position_state; "
                "bad = [m for m in ('redis', 'shared.redis_store', "
                "'strategies.shared_executor', 'shared.position_manager', "
                "'requests', 'psycopg') if m in sys.modules]; print(bad)")
        out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                             text=True, cwd=str(repo), timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == '[]'

    def test_no_real_redis(self):
        """所有断言基于 FakeRedis——结构上无法触网（真实 client 从未被构造）。"""
        adapter, fake = make_adapter()
        adapter.save_positions(POS)
        adapter.load_positions()
        assert fake.get_calls == 1 and len(fake.set_calls) == 1
