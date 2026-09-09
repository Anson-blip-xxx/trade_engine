"""P4-03-01-A：Binance Execution Port + Adapter contract tests。

- 无真实网络：fapi_post 以 fake 注入（Adapter 不自带任何 HTTP）
- 契约：OrderIntent → Adapter → 请求 逐字保持 OrderIntent.to_params()
  （SE Open / PM Open / Full Close / Partial Close 四套 Golden 差异不统一）
"""
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

from execution.adapters.binance import SharedExecutorBinanceAdapter
from execution.core import (
    OrderIntent,
    close_intent,
    partial_close_intent,
    pm_open_intent,
    se_open_intent,
)
from execution.ports.binance import BinanceExecutionPort


class FakeFapiPost:
    """se.fapi_post 同签名 fake：记录调用，可注入结果/异常。"""

    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result
        self.exc = exc

    def __call__(self, path, params=None):
        self.calls.append({'path': path, 'params': params})
        if self.exc is not None:
            raise self.exc
        return self.result


def make_adapter(result=None, exc=None):
    fake = FakeFapiPost(result=result, exc=exc)
    return SharedExecutorBinanceAdapter(fake), fake


# ═══════════════════════════════════════════════════════════════
#  Port 协议
# ═══════════════════════════════════════════════════════════════

class TestPortProtocol:
    def test_adapter_satisfies_port(self):
        adapter, _ = make_adapter()
        assert isinstance(adapter, BinanceExecutionPort)

    def test_port_declares_place_order_only(self):
        """本阶段最小能力：只有 place_order（不为未来预建方法）。"""
        members = [name for name, _ in inspect.getmembers(
            BinanceExecutionPort, predicate=inspect.isfunction)
            if not name.startswith('_')]
        assert members == ['place_order']

    def test_order_path_frozen(self):
        """Adapter 路径冻结：/fapi/v1/order（与 se.open_position 一致）。"""
        assert SharedExecutorBinanceAdapter.ORDER_PATH == '/fapi/v1/order'


# ═══════════════════════════════════════════════════════════════
#  Intent → Adapter → 请求 契约（四套 Golden 差异逐字保持）
# ═══════════════════════════════════════════════════════════════

class TestIntentToRequestContract:
    def test_se_open_intent_params(self):
        adapter, fake = make_adapter(result={'orderId': 1})
        intent = se_open_intent('TESTUSDT', 'LONG', 100.0)
        r = adapter.place_order(intent)
        assert fake.calls == [{'path': '/fapi/v1/order',
                               'params': {'symbol': 'TESTUSDT', 'side': 'BUY',
                                          'type': 'MARKET', 'quantity': 100.0,
                                          'newOrderRespType': 'RESULT'}}]
        assert r == {'orderId': 1}

    def test_se_open_intent_short_no_position_side_no_reduce_only(self):
        adapter, fake = make_adapter()
        adapter.place_order(se_open_intent('TESTUSDT', 'SHORT', 5.0))
        p = fake.calls[0]['params']
        assert p['side'] == 'SELL'
        assert 'positionSide' not in p
        assert 'reduceOnly' not in p
        assert p['newOrderRespType'] == 'RESULT'

    def test_pm_open_intent_params(self):
        adapter, fake = make_adapter()
        adapter.place_order(pm_open_intent('AUSDT', 'SHORT', 10.0))
        p = fake.calls[0]['params']
        assert p == {'symbol': 'AUSDT', 'side': 'SELL', 'type': 'MARKET',
                     'quantity': 10.0, 'positionSide': 'BOTH'}
        assert 'reduceOnly' not in p
        assert 'newOrderRespType' not in p

    def test_full_close_intent_params(self):
        adapter, fake = make_adapter()
        adapter.place_order(close_intent('AUSDT', 'SHORT', 10.0))
        p = fake.calls[0]['params']
        assert p == {'symbol': 'AUSDT', 'side': 'BUY', 'type': 'MARKET',
                     'quantity': 10.0, 'positionSide': 'BOTH',
                     'reduceOnly': 'true'}

    def test_partial_close_intent_params_no_reduce_only(self):
        """E-OBS-5 非对称冻结：partial 无 reduceOnly（Adapter 不"顺便补"）。"""
        adapter, fake = make_adapter()
        adapter.place_order(partial_close_intent('AUSDT', 'LONG', 5.0))
        p = fake.calls[0]['params']
        assert p == {'symbol': 'AUSDT', 'side': 'SELL', 'type': 'MARKET',
                     'quantity': 5.0, 'positionSide': 'BOTH'}
        assert 'reduceOnly' not in p

    def test_params_key_order_preserved(self):
        """params 键序 = to_params() 原序（symbol, side, type, quantity, ...）。"""
        adapter, fake = make_adapter()
        adapter.place_order(se_open_intent('X', 'LONG', 1.0))
        assert list(fake.calls[0]['params'].keys()) == \
            list(se_open_intent('X', 'LONG', 1.0).to_params().keys())

    def test_intent_not_mutated(self):
        adapter, _ = make_adapter()
        intent = se_open_intent('X', 'LONG', 1.0)
        before = intent.to_params()
        adapter.place_order(intent)
        assert intent.to_params() == before


# ═══════════════════════════════════════════════════════════════
#  响应 / 异常语义（不归一化、不包装、不 retry）
# ═══════════════════════════════════════════════════════════════

class TestAdapterSemantics:
    def test_raw_response_verbatim(self):
        raw = {'orderId': 7, 'status': 'FILLED', 'executedQty': '10',
               'avgPrice': '99.5'}
        adapter, _ = make_adapter(result=raw)
        assert adapter.place_order(se_open_intent('X', 'LONG', 10.0)) is raw

    def test_none_semantics_passthrough(self):
        """se.fapi_post 语义：异常内部→None；Adapter 原样返回 None。"""
        adapter, _ = make_adapter(result=None)
        assert adapter.place_order(se_open_intent('X', 'LONG', 1.0)) is None

    def test_reject_response_passthrough(self):
        """拒绝响应原样返回（不解析不包装——解析属 core/调用方）。"""
        raw = {'code': -2019, 'msg': 'Margin is insufficient.'}
        adapter, _ = make_adapter(result=raw)
        assert adapter.place_order(se_open_intent('X', 'LONG', 1.0)) is raw

    def test_exception_propagates_no_wrapping(self):
        """注入 callable 上抛 → Adapter 原样上抛（无包装/无 retry）。"""
        adapter, _ = make_adapter(exc=RuntimeError('network down'))
        with pytest.raises(RuntimeError, match='network down'):
            adapter.place_order(se_open_intent('X', 'LONG', 1.0))

    def test_no_retry_single_call(self):
        adapter, fake = make_adapter(exc=RuntimeError('x'))
        with pytest.raises(RuntimeError):
            adapter.place_order(se_open_intent('X', 'LONG', 1.0))
        assert len(fake.calls) == 1   # 恰好一次，无重试


# ═══════════════════════════════════════════════════════════════
#  依赖边界：ports/adapters 无 IO、不反向依赖 strategies
# ═══════════════════════════════════════════════════════════════

class TestDependencyBoundary:
    FORBIDDEN = ('redis', 'requests', 'binance', 'strategies',
                 'shared_executor', 'position_manager', 'shared.',
                 'psycopg', 'telegram', 'threading', 'queue', 'time',
                 'socket', 'os')

    @pytest.mark.parametrize('module', [
        'execution.ports.binance', 'execution.adapters.binance'])
    def test_source_has_no_forbidden_imports(self, module):
        src = inspect.getsource(__import__(module, fromlist=['x']))
        for mod in self.FORBIDDEN:
            assert not re.search(rf'^\s*(import|from)\s+{re.escape(mod)}',
                                 src, re.M), f'forbidden import: {mod}'

    def test_clean_import_pulls_no_io_modules(self):
        """子进程干净 import ports/adapters → sys.modules 无 IO 模块。"""
        repo = Path(__file__).resolve().parents[2]
        code = ("import sys; "
                "import execution.ports.binance, execution.adapters.binance; "
                "bad = [m for m in ('redis', 'requests', 'psycopg', 'telegram', "
                "'binance', 'strategies.shared_executor', "
                "'shared.position_manager', 'shared.binance_api') "
                "if m in sys.modules]; print(bad)")
        out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                             text=True, cwd=str(repo), timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == '[]'
