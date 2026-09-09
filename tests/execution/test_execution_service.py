"""P4-03-01-A：ExecutionService 骨架 characterization + contract tests。

覆盖（prompt 测试要求 1-8）：
1 Service 可接受 OrderIntent        2 Service 调用 Port
3 传给 Port 的 intent 与原始一致      4 Port 结果原样返回/转换
5 Port 异常 → Service 原样上抛（无 retry/fallback）
6 FakeBinancePort 完整替代真实 Binance
7 Service 不依赖 requests / Binance SDK
8 OrderIntent 字段经 Service 往返保持原样
"""
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

from execution.core import (
    OrderIntent,
    close_intent,
    partial_close_intent,
    pm_open_intent,
    se_open_intent,
)
from execution.service import ExecutionService, OrderExecution


class FakeBinancePort:
    """最小 Fake：完整替代真实 Binance（无网络/无状态泄漏）。"""

    def __init__(self, result=None, exc=None):
        self.received_intents = []
        self.result = result
        self.exc = exc

    def place_order(self, intent):
        self.received_intents.append(intent)
        if self.exc is not None:
            raise self.exc
        return self.result


def make_service(result=None, exc=None):
    fake = FakeBinancePort(result=result, exc=exc)
    return ExecutionService(binance=fake), fake


# ═══════════════════════════════════════════════════════════════
#  T1：Service 接受四套 OrderIntent
# ═══════════════════════════════════════════════════════════════

class TestAcceptsIntents:
    @pytest.mark.parametrize('make_intent', [
        se_open_intent, pm_open_intent, close_intent, partial_close_intent])
    def test_accepts_all_intent_kinds(self, make_intent):
        service, fake = make_service(result={'orderId': 1, 'status': 'FILLED'})
        r = service.execute_order(make_intent('XUSDT', 'SHORT', 3.0))
        assert isinstance(r, OrderExecution)
        assert len(fake.received_intents) == 1

    def test_service_api_minimal(self):
        """本阶段 API 面冻结：只有 execute_order（不提前建 open/close 编排）。"""
        methods = [name for name, _ in inspect.getmembers(
            ExecutionService, predicate=inspect.isfunction)
            if not name.startswith('_')]
        assert methods == ['execute_order']


# ═══════════════════════════════════════════════════════════════
#  T2/T3：调用 Port 且 intent 原样传递
# ═══════════════════════════════════════════════════════════════

class TestIntentPassthrough:
    def test_calls_port_exactly_once(self):
        service, fake = make_service(result={})
        service.execute_order(se_open_intent('X', 'LONG', 1.0))
        assert len(fake.received_intents) == 1

    def test_same_object_identity(self):
        """传给 Port 的 intent 与原始 OrderIntent 是同一对象（无拷贝）。"""
        service, fake = make_service()
        intent = close_intent('AUSDT', 'SHORT', 10.0)
        service.execute_order(intent)
        assert fake.received_intents[0] is intent

    def test_port_receives_intent_not_params(self):
        """Port 接口收 intent（params 映射属 Adapter 层，非 Service 职责）。"""
        service, fake = make_service()
        service.execute_order(pm_open_intent('AUSDT', 'SHORT', 10.0))
        assert isinstance(fake.received_intents[0], OrderIntent)


# ═══════════════════════════════════════════════════════════════
#  T4/T8：结果与字段往返
# ═══════════════════════════════════════════════════════════════

class TestResultRoundTrip:
    def test_raw_response_verbatim_same_object(self):
        raw = {'orderId': 9, 'status': 'FILLED', 'executedQty': '3',
               'cumQty': '3', 'avgPrice': '100.1'}
        service, _ = make_service(result=raw)
        r = service.execute_order(se_open_intent('X', 'LONG', 3.0))
        assert r.raw is raw

    def test_intent_field_values_preserved(self):
        """T8：OrderIntent 字段经 Service 往返逐字段原样（以 core 实际字段为准）。"""
        intent = OrderIntent(
            symbol='AUSDT', side='SELL', type='MARKET', quantity=7.5,
            newOrderRespType='RESULT', positionSide='BOTH', reduceOnly='true')
        service, fake = make_service()
        r = service.execute_order(intent)
        got = fake.received_intents[0]
        assert got.symbol == 'AUSDT'
        assert got.side == 'SELL'
        assert got.quantity == 7.5
        assert got.type == 'MARKET'
        assert got.newOrderRespType == 'RESULT'
        assert got.positionSide == 'BOTH'
        assert got.reduceOnly == 'true'
        assert r.intent is intent

    def test_result_is_frozen(self):
        service, _ = make_service()
        r = service.execute_order(se_open_intent('X', 'LONG', 1.0))
        with pytest.raises(Exception):
            r.raw = {}


class TestRejectedSemantics:
    """rejected = core.is_rejected 原始 truthy 值（N7 冻结，不转 bool）。"""

    def test_filled_not_rejected(self):
        service, _ = make_service(
            result={'orderId': 1, 'status': 'FILLED', 'executedQty': '1'})
        r = service.execute_order(se_open_intent('X', 'LONG', 1.0))
        assert not r.rejected
        assert r.rejected is None          # code 缺失 → get('code') 原值

    def test_code_reject_keeps_code_value(self):
        service, _ = make_service(result={'code': -2019, 'msg': 'insufficient'})
        r = service.execute_order(se_open_intent('X', 'LONG', 1.0))
        assert r.rejected == -2019

    def test_none_raw_rejected_true(self):
        service, _ = make_service(result=None)
        r = service.execute_order(se_open_intent('X', 'LONG', 1.0))
        assert r.rejected is True

    def test_empty_dict_rejected_true(self):
        service, _ = make_service(result={})
        r = service.execute_order(se_open_intent('X', 'LONG', 1.0))
        assert r.rejected is True

    def test_code_zero_not_rejected(self):
        service, _ = make_service(result={'code': 0})
        r = service.execute_order(se_open_intent('X', 'LONG', 1.0))
        assert r.rejected == 0             # falsy 原值


# ═══════════════════════════════════════════════════════════════
#  T5：异常语义（无 retry / fallback / 包装）
# ═══════════════════════════════════════════════════════════════

class TestExceptionSemantics:
    def test_port_exception_propagates_verbatim(self):
        service, fake = make_service(exc=RuntimeError('network down'))
        with pytest.raises(RuntimeError, match='network down'):
            service.execute_order(se_open_intent('X', 'LONG', 1.0))
        assert len(fake.received_intents) == 1   # 恰好一次

    def test_no_retry_on_value_error(self):
        service, fake = make_service(exc=ValueError('bad'))
        with pytest.raises(ValueError):
            service.execute_order(pm_open_intent('X', 'LONG', 1.0))
        assert len(fake.received_intents) == 1


# ═══════════════════════════════════════════════════════════════
#  T6/T7：Fake 完整替代 + 无 IO 依赖
# ═══════════════════════════════════════════════════════════════

class TestIsolation:
    def test_fake_port_full_replacement(self):
        """FakeBinancePort → Service → result 全链路，零网络。"""
        fake = FakeBinancePort(result={'orderId': 42, 'status': 'FILLED',
                                       'executedQty': '10'})
        service = ExecutionService(binance=fake)
        r = service.execute_order(close_intent('AUSDT', 'SHORT', 10.0))
        assert r.raw['orderId'] == 42
        assert not r.rejected
        assert fake.received_intents == [close_intent('AUSDT', 'SHORT', 10.0)]

    def test_service_stateless_between_calls(self):
        service, fake = make_service(result={'ok': 1})
        r1 = service.execute_order(se_open_intent('A', 'LONG', 1.0))
        r2 = service.execute_order(close_intent('B', 'SHORT', 2.0))
        assert r1.raw is r2.raw
        assert r1.intent is not r2.intent
        assert [i.symbol for i in fake.received_intents] == ['A', 'B']


class TestDependencyBoundary:
    FORBIDDEN = ('redis', 'requests', 'binance', 'strategies',
                 'shared_executor', 'position_manager', 'shared.',
                 'psycopg', 'telegram', 'threading', 'queue', 'time',
                 'socket', 'os')

    def test_service_source_has_no_forbidden_imports(self):
        src = inspect.getsource(__import__('execution.service',
                                           fromlist=['x']))
        for mod in self.FORBIDDEN:
            assert not re.search(rf'^\s*(import|from)\s+{re.escape(mod)}',
                                 src, re.M), f'forbidden import: {mod}'

    def test_clean_import_pulls_no_io_modules(self):
        """子进程干净 import service/ports/adapters → 无 requests/redis/strategies。"""
        repo = Path(__file__).resolve().parents[2]
        code = ("import sys; "
                "import execution.service, execution.ports.binance, "
                "execution.adapters.binance; "
                "bad = [m for m in ('redis', 'requests', 'psycopg', 'telegram', "
                "'binance', 'strategies.shared_executor', "
                "'shared.position_manager', 'shared.binance_api') "
                "if m in sys.modules]; print(bad)")
        out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                             text=True, cwd=str(repo), timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == '[]'
