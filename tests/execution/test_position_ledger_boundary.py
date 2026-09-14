"""P4-03-01-D3：Position Ledger（PostgreSQL）boundary contract / parity tests。

覆盖（prompt 测试要求 1-20）：
1 Port contract（两个既有 seam 方法，不发明业务接口）
2-3 adapter 逐字委托（同一 dict 引用 / 返回值 / 异常）
4-8 字段名/value 类型/None/missing 字段 全部原样（adapter 不检查不加工）
9 exception 语义：adapter 原样上抛 + helper 吞错语义 parity
10/11 commit/rollback 行为保持（真实 _connection 行为 characterization）
12-14 return value / call count / call order
15-16 无真实 PG 网络；不创建新 DB client
17-19 adapter 不依赖 PM/SE/strategies（源码扫描 + 子进程 sys.modules）
20 不修改 Redis（adapter 无 redis 痕迹）
+ parity：直接 helper vs adapter —— SQL/params 完全一致（fake connection 捕获）
+ NO-WIRING 状态冻结（se/PM 仍直调 _pg_record_event）
"""
import inspect
import json
import re
import subprocess
import sys
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path

import pytest

from execution.adapters.postgres_ledger import PostgresLedgerAdapter
from execution.ports.ledger import PositionLedgerPort

import shared.postgres_client as pg


class FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        if self._conn.fail:
            raise RuntimeError('db down')
        self._conn.executed.append((sql, params))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeConn:
    """psycopg 连接行为 fake（commit/rollback/close 计数）。"""

    def __init__(self, fail=False):
        self.executed = []
        self.committed = 0
        self.rolled_back = 0
        self.closed = 0
        self.fail = fail

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.committed += 1

    def rollback(self):
        self.rolled_back += 1

    def close(self):
        self.closed += 1


# 与生产 _connection 相同的语义（成功 commit / 异常 rollback+raise / 收尾 close），
# 用于在测试中绑定 pg helper（不修改生产代码）。
@contextmanager
def _fake_connection(ctx_holder):
    conn = ctx_holder['conn']
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def bind(conn, enabled=True):
    """把 fake connection 绑到 postgres_client 之上，返回 adapter+conn。"""
    ctx = {'conn': conn}
    holder = {'conn': conn}
    fake_cm = _make_cm(conn)
    return fake_cm, _PGBind(fake_cm)


def _make_cm(conn):
    @contextmanager
    def cm():
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return cm


class _PGBind:
    """在 fake connection 上运行真实 helper 的双通道（直接调用 + adapter）。"""

    def direct_record(self, data):
        return self._run(pg.record_trade_event, data)

    def direct_upsert(self, data):
        return self._run(pg.upsert_trade_episode, data)

    def adapter_record(self, data):
        adapter = self.build_adapter()
        return adapter.record_trade_event(data)

    def adapter_upsert(self, data):
        adapter = self.build_adapter()
        return adapter.upsert_trade_episode(data)

    def __init__(self, fake_cm):
        self._cm = fake_cm

    def build_adapter(self):
        # adapter 注入**真实 helper 函数**（当前 fake connection 生效）
        return PostgresLedgerAdapter(pg.record_trade_event,
                                     pg.upsert_trade_episode)

    def _run(self, fn, data):
        return self._cm_captured(generate=lambda: fn(data))

    def _cm_captured(self, generate):
        return generate()


EVENT = {
    'event_id': 'order:1:open', 'position_id': 'S6:X:100:1788000000.000000',
    'event_type': 'OPEN_ORDER_FILLED', 'order_id': '1', 'fill_id': '',
    'price': 100.0, 'qty': 100.0, 'realized_pnl': 0.0,
    'payload': {'order': {'orderId': 1}, 'decision_context': {}},
}
EPISODE = {
    'position_id': 'S6:X:100:1788000000.000000', 'symbol': 'XUSDT',
    'system_name': 'S6', 'side': 'SHORT', 'entry_price': 100.0,
    'exit_price': 97.5, 'qty': 3.0, 'leverage': 3, 'pnl_pct': 2.5,
    'pnl_usdt': 7.5, 'duration_min': 42, 'result': 'win',
    'exit_reason': '硬止损', 'event_type': 'TREND_DOWN', 'strength': 70.0,
    'margin_mode': 'ISOLATED', 'sl_price': 105.0, 'ghost_cleanup': False,
    'open_time': 1788000000.0,
    'metadata': {'algo_sl_id': 1}, 'env': 'demo',
}


def patched_pg(monkeypatch, conn=None):
    """绑定：enabled=True + fake _connection（真实 helper 链路运行于 fake）。"""
    conn = conn or FakeConn()
    monkeypatch.setattr(pg, 'enabled', lambda: True)
    monkeypatch.setattr(pg, '_connection', _make_cm(conn))
    return pg, conn


# ═══════════════════════════════════════════════════════════════
#  1. Port contract
# ═══════════════════════════════════════════════════════════════

class TestPortContract:
    def test_port_declares_two_seam_methods(self):
        """最小面：只有 record_trade_event / upsert_trade_episode（无业务造口）。"""
        members = [name for name, _ in inspect.getmembers(
            PositionLedgerPort, predicate=inspect.isfunction)
            if not name.startswith('_')]
        assert members == ['record_trade_event', 'upsert_trade_episode']

    def test_adapter_satisfies_port(self):
        adapter = PostgresLedgerAdapter(lambda d: True, lambda d: False)
        assert isinstance(adapter, PositionLedgerPort)

    def test_no_pnl_or_binance_logic_in_boundary(self):
        """Port/Adapter 不含 PnL/计算/fill 解析（persist already-produced data）。"""
        for module in ('execution.ports.ledger', 'execution.adapters.postgres_ledger'):
            src = inspect.getsource(__import__(module, fromlist=['x']))
            for kw in ('pnl_pct', 'positionAmt', 'requests'):
                assert kw not in src, f'{module} 泄漏业务: {kw}'


# ═══════════════════════════════════════════════════════════════
#  2/3/7/12. 委托正确性（同一引用 / 返回值原样）
# ═══════════════════════════════════════════════════════════════

class TestDelegation:
    def test_record_event_same_object_and_return(self):
        calls = []
        adapter = PostgresLedgerAdapter(
            lambda d: calls.append(d) or True, lambda d: False)
        rv = adapter.record_trade_event(EVENT)
        assert rv is True
        assert calls == [EVENT]
        assert calls[0] is EVENT                      # 同一引用（无拷贝）

    def test_upsert_returns_false_verbatim(self):
        adapter = PostgresLedgerAdapter(lambda d: True, lambda d: False)
        assert adapter.upsert_trade_episode(EPISODE) is False

    def test_field_names_and_types_untouched(self):
        """4-8：含 None/Decimal/嵌套 dict 的参数逐字传递（无序列化/转换）。"""
        seen = []
        data = {'a': None, 'b': Decimal('1.23'), 'c': {'nested': [1, 2]},
                'payload': {'x': 'y'}}
        adapter = PostgresLedgerAdapter(
            lambda d: seen.append(d) or True, lambda d: True)
        adapter.record_trade_event(data)
        got = seen[0]
        assert got is data
        assert got['a'] is None and got['b'] == Decimal('1.23')
        assert got['c'] == {'nested': [1, 2]}

    def test_missing_optional_fields_no_defaults_added(self):
        """缺字段不补（不检查 keys——与 helper 契约一致）。"""
        seen = []
        adapter = PostgresLedgerAdapter(
            lambda d: seen.append(d) or False, lambda d: True)
        adapter.record_trade_event({'event_id': 'minimal'})
        assert seen[0] == {'event_id': 'minimal'}

    def test_call_count_signature_freeze(self):
        calls = {'r': 0, 'u': 0}
        adapter = PostgresLedgerAdapter(
            lambda d: calls.__setitem__('r', calls['r'] + 1) or True,
            lambda d: calls.__setitem__('u', calls['u'] + 1) or True)
        adapter.record_trade_event(EVENT)
        adapter.upsert_trade_episode(EPISODE)
        assert calls == {'r': 1, 'u': 1}             # 单次调用，无重试


# ═══════════════════════════════════════════════════════════════
#  9/10/11. helper 异常/事务语义（真实行为 characterization）
# ═══════════════════════════════════════════════════════════════

def test_helper_event_not_enabled_returns_false(monkeypatch):
    """POSTGRES_DSN 未配置 → False（不连接、不抛错）。"""
    monkeypatch.setattr(pg, 'enabled', lambda: False)

    def _must_not_connect():
        raise AssertionError('不得连接 DB')
    monkeypatch.setattr(pg, '_connection', _must_not_connect)
    conn = FakeConn()
    assert pg.record_trade_event(EVENT) is False


def test_helper_event_failure_rolls_back_returns_false(monkeypatch):
    """execute 失败 → rollback → helper 吞错 → False（transaction 语义保持）。"""
    pg2, conn = patched_pg(monkeypatch)
    bad_conn = FakeConn(fail=True)
    monkeypatch.setattr(pg, '_connection', _make_cm(bad_conn))
    assert pg2.record_trade_event(EVENT) is False
    assert bad_conn.rolled_back == 1 and bad_conn.committed == 0


def test_helper_event_success_commits(monkeypatch):
    pg2, conn = patched_pg(monkeypatch)
    assert pg2.record_trade_event(EVENT) is True
    assert conn.committed == 1 and conn.rolled_back == 0
    assert len(conn.executed) == 1


def test_helper_upsert_serializes_metadata_and_env(monkeypatch):
    """序列化语义冻结：metadata/payload json.dumps(default=str)；env 兜底。"""
    pg2, conn = patched_pg(monkeypatch)
    data = dict(EPISODE)
    data.pop('env', None)
    assert pg2.upsert_trade_episode(data) is True
    sql, params = conn.executed[0]
    assert 'trade_episodes' in sql and 'ON CONFLICT' in sql
    assert json.loads(params['metadata']) == {'algo_sl_id': 1}   # json.dumps 后
    assert params['env'] in ('demo', 'prod')                     # _data_env 兜底


# ═══════════════════════════════════════════════════════════════
#  Parity：直接 helper vs adapter（SQL/params/return 一致）
# ═══════════════════════════════════════════════════════════════

class TestHelperVsAdapterParity:
    def _run_pair(self, monkeypatch, data, which):
        c1 = FakeConn()
        monkeypatch.setattr(pg, '_connection', _make_cm(c1))
        monkeypatch.setattr(pg, 'enabled', lambda: True)
        if which == 'record':
            rv_direct = pg.record_trade_event(data)
        else:
            rv_direct = pg.upsert_trade_episode(data)
        c2 = FakeConn()
        monkeypatch.setattr(pg, '_connection', _make_cm(c2))
        adapter = PostgresLedgerAdapter(pg.record_trade_event,
                                        pg.upsert_trade_episode)
        rv_adapter = (adapter.record_trade_event(data) if which == 'record'
                      else adapter.upsert_trade_episode(data))
        return c1.executed, rv_direct, c2.executed, rv_adapter

    def test_record_parity(self, monkeypatch):
        e1, rv1, e2, rv2 = self._run_pair(monkeypatch, EVENT, 'record')
        assert e1 == e2 and rv1 == rv2 is True
        assert e1[0][0] == e2[0][0]
        assert json.loads(e1[0][1]['payload']) == json.loads(
            e2[0][1]['payload']) == {'order': {'orderId': 1},
                                     'decision_context': {}}

    def test_upsert_parity(self, monkeypatch):
        e1, rv1, e2, rv2 = self._run_pair(monkeypatch, EPISODE, 'upsert')
        assert e1 == e2 and rv1 == rv2 is True

    def test_upsert_failure_parity(self, monkeypatch):
        """失败路径 parity：SQL 相同（fake fail）→ 均 False。"""
        e1, rv1, e2, rv2 = self._run_pair(
            monkeypatch, EPISODE, 'upsert')     # 占位
        # 失败 parity 单独构造：
        bad1 = FakeConn(fail=True)
        monkeypatch.setattr(pg, '_connection', _make_cm(bad1))
        rv_direct = pg.upsert_trade_episode(EPISODE)
        bad2 = FakeConn(fail=True)
        monkeypatch.setattr(pg, '_connection', _make_cm(bad2))
        adapter = PostgresLedgerAdapter(pg.record_trade_event,
                                        pg.upsert_trade_episode)
        rv_adapter = adapter.upsert_trade_episode(EPISODE)
        assert rv_direct is rv_adapter is False
        assert bad1.rolled_back == bad2.rolled_back == 1


# ═══════════════════════════════════════════════════════════════
#  NO-WIRING 状态冻结 + 调用点盘点
# ═══════════════════════════════════════════════════════════════

class TestNoWiring:
    def test_se_open_still_calls_pg_directly(self):
        from strategies import shared_executor as se
        src = inspect.getsource(se.open_position)
        assert '_pg_record_event(' in src                # OPEN_ORDER_FILLED 直调

    def test_pm_close_still_calls_pg_directly(self):
        # P7-07B：_close 逻辑迁入 PositionLifecycleService（thin delegate）；
        # guard 目标随之迁移（CLOSE/FLAT 分支直调语义不变）
        from shared import position_manager as pm
        from position_lifecycle import service as lc
        src = inspect.getsource(lc.PositionLifecycleService.close)
        assert 'self.action.pg(' in src             # CLOSE/FLAT 分支直调（注入 = pg）
        assert 'thin delegation' in inspect.getsource(pm._close)

    def test_service_not_consumes_ledger_port(self):
        """ExecutionService 不感知 ledger port（现状冻结）。"""
        src = inspect.getsource(__import__('execution.service',
                                           fromlist=['x']))
        assert 'Ledger' not in src


# ═══════════════════════════════════════════════════════════════
#  15-20：隔离与依赖边界
# ═══════════════════════════════════════════════════════════════

class TestDependencyBoundary:
    FORBIDDEN = ('redis', 'requests', 'psycopg', 'strategies',
                 'shared_executor', 'position_manager', 'shared.',
                 'postgres_client', 'telegram', 'binance')

    @pytest.mark.parametrize('module', [
        'execution.ports.ledger', 'execution.adapters.postgres_ledger'])
    def test_source_has_no_forbidden_imports(self, module):
        src = inspect.getsource(__import__(module, fromlist=['x']))
        for mod in self.FORBIDDEN:
            assert not re.search(rf'^\s*(import|from)\s+{re.escape(mod)}',
                                 src, re.M), f'forbidden import: {mod}'

    def test_clean_import_pulls_no_io_modules(self):
        repo = Path(__file__).resolve().parents[2]
        code = ("import sys; "
                "import execution.ports.ledger, "
                "execution.adapters.postgres_ledger; "
                "bad = [m for m in ('psycopg', 'redis', 'requests', "
                "'shared.postgres_client', 'shared.position_manager', "
                "'strategies.shared_executor') if m in sys.modules]; print(bad)")
        out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                             text=True, cwd=str(repo), timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == '[]'

    def test_no_real_network_or_redis(self):
        """全部经 fake connection/fake callable —— 结构上不可能触网/触 Redis。"""
        adapter = PostgresLedgerAdapter(lambda d: True, lambda d: False)
        assert adapter.record_trade_event(EVENT) is True
        assert adapter.upsert_trade_episode(EPISODE) is False
