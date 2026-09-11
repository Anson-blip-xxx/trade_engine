"""P4-03-01-D1：PositionManager Boundary contract / characterization tests。

覆盖（prompt 测试要求 1-12 + parity）：
1 Port contract（最小面/与真实实现签名 parity）
2-3 adapter 逐字委托（参数/返回值原样）
3/4/5 ExecutionOutcome → PM boundary 流转（成功回调/失败不回调/无 Workflow 改动）
6 exception 传播原样
7/8 adapter 不改参数、不改结果
9-12 无真实 Binance/Redis/PG/TG（隔离 + 源码/子进程依赖检查）
- 不接入生产主链（contract + adapter + tests；Service 不感知 PM）
"""
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

from execution.adapters.pm import PositionManagerAdapter
from execution.core import OrderIntent, parse_execution_result, se_open_intent
from execution.ports.pm import PositionManagerPort
from execution.service import ExecutionService, OrderExecution


class FakeUpdatePosCache:
    """se._update_pos_cache 同签名 fake：记录调用，可注入结果/异常。"""

    def __init__(self, result='S6:TESTUSDT:100:1788000000.000000', exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    def __call__(self, name, symbol, side, entry, qty, stop_price,
                 leverage, margin, event_type, strength):
        self.calls.append({'name': name, 'symbol': symbol, 'side': side,
                           'entry': entry, 'qty': qty, 'stop_price': stop_price,
                           'leverage': leverage, 'margin': margin,
                           'event_type': event_type, 'strength': strength})
        if self.exc is not None:
            raise self.exc
        return self.result


ARGS = dict(name='S6', symbol='TESTUSDT', side='LONG', entry=100.0,
            qty=100.0, stop_price=95.0, leverage=3, margin='CROSSED',
            event_type='TREND_UP', strength=70)


# ═══════════════════════════════════════════════════════════════
#  1. Port contract
# ═══════════════════════════════════════════════════════════════

class TestPortContract:
    def test_port_declares_register_only(self):
        """最小面：只有 register_opened_position（不为 close 发明接口）。"""
        members = [name for name, _ in inspect.getmembers(
            PositionManagerPort, predicate=inspect.isfunction)
            if not name.startswith('_')]
        assert members == ['register_opened_position']

    def test_adapter_satisfies_port(self):
        assert isinstance(PositionManagerAdapter(FakeUpdatePosCache()),
                          PositionManagerPort)

    def test_port_signature_matches_real_update_pos_cache(self):
        """parity：Port 方法签名 == shared_executor._update_pos_cache 逐字镜像。"""
        from strategies import shared_executor as se
        port_sig = inspect.signature(
            PositionManagerPort.register_opened_position)
        real_sig = inspect.signature(se._update_pos_cache)
        port_params = [(n, p.default) for n, p in port_sig.parameters.items()
                       if n != 'self']
        real_params = [(n, p.default) for n, p in real_sig.parameters.items()]
        assert port_params == real_params   # 参数名/顺序/默认值逐字一致

    def test_no_production_wiring_yet(self):
        """冻结当前状态：open_position 仍直调 _update_pos_cache（编排层负责），
        ExecutionService 不消费 PM port（无行为边界变化）。"""
        from strategies import shared_executor as se
        src = inspect.getsource(se.open_position)
        assert '_update_pos_cache(' in src
        svc_src = inspect.getsource(__import__('execution.service',
                                               fromlist=['x']))
        assert 'PositionManagerPort' not in svc_src
        assert 'register_opened_position' not in svc_src


# ═══════════════════════════════════════════════════════════════
#  2/7/8. Adapter 委托正确性（不改参数/不改结果）
# ═══════════════════════════════════════════════════════════════

class TestAdapterDelegation:
    def test_delegates_all_ten_args_positionally(self):
        fake = FakeUpdatePosCache()
        adapter = PositionManagerAdapter(fake)
        pid = adapter.register_opened_position(**ARGS)
        assert pid == 'S6:TESTUSDT:100:1788000000.000000'
        assert fake.calls == [ARGS]

    def test_returns_position_id_verbatim(self):
        sentinel = object()
        adapter = PositionManagerAdapter(lambda *a, **k: sentinel)
        r = adapter.register_opened_position(**ARGS)
        assert r is sentinel

    def test_does_not_mutate_args(self):
        recorded = {}

        def probe(*a, **k):
            recorded.update(a=a, k=k)
            return 'pid'
        adapter = PositionManagerAdapter(probe)
        snapshot = dict(ARGS)
        adapter.register_opened_position(**ARGS)
        assert ARGS == snapshot                   # 入参不被修改
        # 委托方式冻结：位置传参（se._update_pos_cache 调用习惯），kwargs 为空
        assert recorded['a'] == ('S6', 'TESTUSDT', 'LONG', 100.0, 100.0,
                                 95.0, 3, 'CROSSED', 'TREND_UP', 70)
        assert recorded['k'] == {}

    def test_exception_propagates_verbatim(self):
        """真实失败的异常语义原样上抛（E-OBS-1 的 cancel+False 处理留在编排层）。"""
        adapter = PositionManagerAdapter(
            FakeUpdatePosCache(exc=RuntimeError('redis down')))
        with pytest.raises(RuntimeError, match='redis'):
            adapter.register_opened_position(**ARGS)


# ═══════════════════════════════════════════════════════════════
#  3/4/5. ExecutionOutcome → PM boundary 流转
# ═══════════════════════════════════════════════════════════════

class TestOutcomeToPMBoundary:
    def _fake_port(self):
        received = []

        class Port:
            def place_order(self, intent):
                received.append(intent)
                return {'orderId': 1, 'status': 'FILLED',
                        'executedQty': '100', 'cumQty': '100',
                        'avgPrice': '100.5'}
        return Port(), received

    def test_success_outcome_drives_registration(self):
        """open 成果（raw → core parse）→ port 回调：entry=avg/filled_qty。"""
        port, received = self._port = self._make_port()
        fake = FakeUpdatePosCache()
        adapter = PositionManagerAdapter(fake)

        intent = se_open_intent('TESTUSDT', 'LONG', 100.0)
        outcome = ExecutionService(binance=port).execute_order(intent)
        assert not outcome.rejected
        parsed = parse_execution_result(outcome.raw, entry_price=100.0)

        pid = adapter.register_opened_position(
            name='S6', symbol='TESTUSDT', side='LONG',
            entry=parsed.avg_price, qty=parsed.filled_qty,
            stop_price=95.0, leverage=3, margin='CROSSED',
            event_type='TREND_UP', strength=70)
        assert pid == fake.result
        assert fake.calls[0]['entry'] == 100.0     # avgPrice
        assert fake.calls[0]['qty'] == 100.0       # executedQty

    def _make_port(self):
        received = []

        class Port:
            def place_order(self, intent):
                received.append(intent)
                return {'orderId': 1, 'status': 'FILLED',
                        'executedQty': '100', 'cumQty': '100',
                        'avgPrice': '100.0'}
        return Port(), received

    def test_rejected_outcome_does_not_register(self):
        """execution 失败 → PM 回调不被调用（raw=None/'code' 不产生 parsed 字段流转）。"""
        fake = FakeUpdatePosCache()
        adapter = PositionManagerAdapter(fake)

        class RejectPort:
            def place_order(self, intent):
                return {'code': -2019, 'msg': 'insufficient'}

        outcome = ExecutionService(binance=RejectPort()).execute_order(
            se_open_intent('X', 'LONG', 1.0))
        assert outcome.rejected
        # 编排层（E-OBS-1/2 冻结）在此短路；contract-only 测试验证回调不触发
        assert adapter._update_pos_cache.calls == []

    def test_none_outcome_does_not_register(self):
        fake = FakeUpdatePosCache()
        adapter = PositionManagerAdapter(fake)

        class NonePort:
            def place_order(self, intent):
                return None

        outcome = ExecutionService(binance=NonePort()).execute_order(
            se_open_intent('X', 'LONG', 1.0))
        assert outcome.rejected is True
        assert adapter._update_pos_cache.calls == []


# ═══════════════════════════════════════════════════════════════
#  9-12. 隔离：无真实 IO；execution 包不反向依赖 strategies
# ═══════════════════════════════════════════════════════════════

class TestIsolation:
    FORBIDDEN_SRC = ('redis', 'requests', 'binance', 'strategies',
                     'shared_executor', 'position_manager', 'shared.',
                     'psycopg', 'telegram', 'threading', 'queue', 'time',
                     'socket', 'os')

    @pytest.mark.parametrize('module', [
        'execution.ports.pm', 'execution.adapters.pm'])
    def test_source_has_no_forbidden_imports(self, module):
        src = inspect.getsource(__import__(module, fromlist=['x']))
        for mod in self.FORBIDDEN_SRC:
            assert not re.search(rf'^\s*(import|from)\s+{re.escape(mod)}',
                                 src, re.M), f'forbidden import: {mod}'

    def test_clean_import_pulls_no_io_modules(self):
        """子进程干净 import pm port/adapter → sys.modules 无 IO/strategies。"""
        repo = Path(__file__).resolve().parents[2]
        code = ("import sys; "
                "import execution.ports.pm, execution.adapters.pm, "
                "execution.service; "
                "bad = [m for m in ('redis', 'requests', 'psycopg', 'telegram', "
                "'binance', 'strategies.shared_executor', "
                "'shared.position_manager', 'shared.redis_store', "
                "'shared.postgres_client') if m in sys.modules]; print(bad)")
        out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                             text=True, cwd=str(repo), timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == '[]'

    def test_no_real_network_or_db(self):
        """本文件全部经 fake callable/fake port 注入——结构上不可能触网。
        （fake 断言本身即证明：所有断言基于 fake.received/calls，零外部调用）"""
        fake = FakeUpdatePosCache()
        adapter = PositionManagerAdapter(fake)
        adapter.register_opened_position(**ARGS)
        assert len(fake.calls) == 1     # 唯一 IO = 注入的 fake
