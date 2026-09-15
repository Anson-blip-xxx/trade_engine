"""P4-02：Execution Core characterization + parity tests。

- characterization：冻结 execution.core 自身的输入输出
- parity：core 输出 vs 原始实现（PM 未改动 → 真实对照；se 已替换的内核
  用"逐字复制的原始表达式"作为 REFERENCE 对照）

隔离：无真实网络/Redis/PG/TG；不启动真实 algo worker。
"""
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

from execution import core as exec_core


# ── 参照实现（逐字复制自 P4-02 提取前的真实代码，仅用于 parity） ────────

def REF_se_round_qty_kernel(qty: float, step: float) -> float:
    """原 se._round_qty LOT_SIZE 分支（se L1066-1070 提取前逐字复制）。"""
    step_str = str(step).rstrip('0')
    decimals = len(step_str.split('.')[1]) if '.' in step_str else 0
    return round(qty - (qty % step), decimals)


def REF_parse(result: dict, entry_price: float):
    """原 se.open_position 解析段（提取前逐字复制）。"""
    status = result.get('status', 'NEW')
    filled_qty = abs(float(result.get('executedQty', 0)))
    cum_qty = abs(float(result.get('cumQty', filled_qty)))
    avg_price_str = result.get('avgPrice', '0')
    avg_price = float(avg_price_str) if avg_price_str and float(avg_price_str) > 0 \
        else entry_price
    return status, filled_qty, cum_qty, avg_price


def REF_pm_pnl(side: str, entry: float, price: float, close_qty: float):
    """原 pm._close pnl 段（提取前逐字复制）。"""
    if side == 'SHORT':
        pnl_pct = (entry - price) / entry * 100
        pnl_u = round((entry - price) * close_qty, 2)
    else:
        pnl_pct = (price - entry) / entry * 100
        pnl_u = round((price - entry) * close_qty, 2)
    return pnl_pct, pnl_u


def _pos(**kw):
    base = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
            'system': 'S8', 'open_time': 1788000000, 'leverage': 3,
            'sl': 2.2, 'signal_type': 'TREND_DOWN', 'event_type': 'TREND_DOWN',
            'score': 66, 'be_done': False}
    base.update(kw)
    return base


def _make_open_kwargs(**kw):
    """open_position 标准调用参数（与 tests/execution/conftest.py 保持一致）。"""
    base = {'name': 'S6', 'symbol': 'TESTUSDT', 'side': 'LONG',
            'entry_price': 100.0, 'stop_price': 95.0, 'qty': 100.0,
            'margin_mode': 'CROSSED', 'leverage': 3,
            'event_type': 'TREND_UP', 'strength': 70, 'tg_fn': None,
            'expected_move_pct': 0, 'decision_context': {}}
    base.update(kw)
    return base


def _order_posts(calls):
    return [p for p in calls['post'] if p['path'] == '/fapi/v1/order']


# ═══════════════════════════════════════════════════════════════
#  Side 映射
# ═══════════════════════════════════════════════════════════════

class TestOrderSideMapping:
    def test_long_to_buy(self):
        assert exec_core.order_side_for('LONG') == 'BUY'

    def test_short_to_sell(self):
        assert exec_core.order_side_for('SHORT') == 'SELL'

    def test_non_short_is_buy(self):
        """真实代码是严格 == 'SHORT' 判断：'long'/''/None 等一律 BUY。"""
        assert exec_core.order_side_for('long') == 'BUY'
        assert exec_core.order_side_for('') == 'BUY'
        assert exec_core.order_side_for('whatever') == 'BUY'

    @pytest.mark.parametrize('side', ['LONG', 'SHORT', 'long', '', 'x'])
    def test_parity_reference(self, side):
        ref = 'SELL' if side == 'SHORT' else 'BUY'
        assert exec_core.order_side_for(side) == ref


# ═══════════════════════════════════════════════════════════════
#  Order Intent（字段冻结）
# ═══════════════════════════════════════════════════════════════

class TestOrderIntent:
    def test_se_open_intent_fields(self):
        """se 开仓意图：无 positionSide / reduceOnly，含 RESULT。"""
        p = exec_core.se_open_intent('TESTUSDT', 'LONG', 100.0).to_params()
        assert p == {'symbol': 'TESTUSDT', 'side': 'BUY', 'type': 'MARKET',
                     'quantity': 100.0, 'newOrderRespType': 'RESULT'}
        assert 'positionSide' not in p
        assert 'reduceOnly' not in p

    def test_se_open_intent_short(self):
        p = exec_core.se_open_intent('XUSDT', 'SHORT', 5.0).to_params()
        assert p['side'] == 'SELL'

    def test_pm_open_intent_fields(self):
        """pm 开仓意图：positionSide=BOTH，无 reduceOnly / RESULT。"""
        p = exec_core.pm_open_intent('AUSDT', 'SHORT', 10.0).to_params()
        assert p == {'symbol': 'AUSDT', 'side': 'SELL', 'type': 'MARKET',
                     'quantity': 10.0, 'positionSide': 'BOTH'}
        assert 'newOrderRespType' not in p
        assert 'reduceOnly' not in p

    def test_close_intent_fields(self):
        """pm._close 意图：positionSide=BOTH + reduceOnly='true'（E-OBS-5）。"""
        p = exec_core.close_intent('AUSDT', 'SHORT', 10.0).to_params()
        assert p == {'symbol': 'AUSDT', 'side': 'BUY', 'type': 'MARKET',
                     'quantity': 10.0, 'positionSide': 'BOTH',
                     'reduceOnly': 'true'}

    def test_close_intent_long(self):
        p = exec_core.close_intent('AUSDT', 'LONG', 7.0).to_params()
        assert p['side'] == 'SELL'
        assert p['reduceOnly'] == 'true'

    def test_partial_close_intent_no_reduce_only(self):
        """pm._partial_close 意图：无 reduceOnly（与 _close 的真实非对称）。"""
        p = exec_core.partial_close_intent('AUSDT', 'SHORT', 4.0).to_params()
        assert p == {'symbol': 'AUSDT', 'side': 'BUY', 'type': 'MARKET',
                     'quantity': 4.0, 'positionSide': 'BOTH'}
        assert 'reduceOnly' not in p

    def test_intent_frozen(self):
        """OrderIntent 不可变。"""
        intent = exec_core.se_open_intent('A', 'LONG', 1.0)
        with pytest.raises(Exception):
            intent.quantity = 2.0

    # ── parity：core vs 真实生产路径捕获 ──

    def test_parity_se_open_order_params(self, exec_env):
        """core builder == se.open_position 真实发出的 params。"""
        se = exec_env['se']
        se.open_position(**_make_open_kwargs())
        captured = _order_posts(exec_env['calls'])[0]['params']
        expect = exec_core.se_open_intent('TESTUSDT', 'LONG', 100.0).to_params()
        assert captured == expect
        assert list(captured.keys()) == list(expect.keys())  # 键序也一致

    def test_parity_se_open_order_params_short(self, exec_env):
        se = exec_env['se']
        se.open_position(**_make_open_kwargs(side='SHORT'))
        captured = _order_posts(exec_env['calls'])[0]['params']
        assert captured == exec_core.se_open_intent(
            'TESTUSDT', 'SHORT', 100.0).to_params()

    def test_parity_pm_open_order_params(self, close_env, monkeypatch):
        """core builder == pm.open_position 真实发出的 params（PM 未改）。"""
        pm = close_env['pm']

        def fapi_post(path, params=None):
            close_env['calls']['post'].append({'path': path, 'params': params})
            if 'order' in path:
                return {'orderId': 1, 'status': 'FILLED',
                        'executedQty': str(params['quantity'])}
            return {}
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda p, q=None: [], fapi_post, lambda *a, **k: {},
            lambda sym: 2.0, lambda sym: (6, 6),
            lambda *a, **k: (0, 0, 0), lambda *a, **k: 50.0,
            lambda *a, **kw: None))
        monkeypatch.setattr(pm, '_algo_start_worker', lambda: None)
        # P9-03B：precheck 需要 empty local state（order-only path测试）
        monkeypatch.setattr(pm, '_load', lambda: {})

        r = pm.open_position('AUSDT', 'SHORT', 2.0, 10.0, 3, 2.2)
        assert r is True
        orders = [p for p in close_env['calls']['post'] if 'order' in p['path']]
        assert orders[0]['params'] == exec_core.pm_open_intent(
            'AUSDT', 'SHORT', 10.0).to_params()
        pm._ALGO_QUEUE.clear()

    def test_parity_pm_close_order_params(self, close_env):
        """core builder == pm._close 真实发出的 params（PM 未改）。"""
        pm = close_env['pm']
        pos = _pos()
        positions = {'AUSDT': pos}
        assert pm._close('AUSDT', pos, 1.9, '硬止损', positions) is True
        orders = [p for p in close_env['calls']['post'] if 'order' in p['path']]
        assert orders[0]['params'] == exec_core.close_intent(
            'AUSDT', 'SHORT', 10.0).to_params()

    def test_parity_pm_partial_close_order_params(self, close_env):
        pm = close_env['pm']
        pos = _pos(side='LONG', entry=1.0, qty=10.0, original_qty=10.0)
        positions = {'AUSDT': pos}
        pm._partial_close('AUSDT', pos, 1.1, 5.0, 5, positions)
        orders = [p for p in close_env['calls']['post'] if 'order' in p['path']]
        assert orders[0]['params'] == exec_core.partial_close_intent(
            'AUSDT', 'LONG', 5.0).to_params()


# ═══════════════════════════════════════════════════════════════
#  Response parsing
# ═══════════════════════════════════════════════════════════════

class TestIsRejected:
    def test_none_is_rejected(self):
        assert exec_core.is_rejected(None) is True

    def test_empty_dict_is_rejected(self):
        """冻结：空 dict → not result 为 True → 视为拒绝（真实行为）。"""
        assert exec_core.is_rejected({}) is True

    def test_code_rejected(self):
        # 冻结：返回 code 原值（truthy），非布尔 True
        r = exec_core.is_rejected({'code': -2019, 'msg': 'insufficient'})
        assert r == -2019
        assert r

    def test_code_none_not_rejected(self):
        assert not exec_core.is_rejected({'code': None})
        assert exec_core.is_rejected({'code': None}) is None

    def test_normal_filled_not_rejected(self):
        assert not exec_core.is_rejected({'orderId': 1, 'status': 'FILLED'})

    @pytest.mark.parametrize('result', [None, {}, {'code': -1111},
                                        {'code': None}, {'status': 'FILLED'},
                                        {'code': 0}])
    def test_parity_reference(self, result):
        ref = not result or result.get('code')
        assert exec_core.is_rejected(result) == ref

    def test_code_zero_not_rejected(self):
        """code=0 → falsy → 不拒绝（真实短路语义）。"""
        assert not exec_core.is_rejected({'code': 0})


class TestParseExecutionResult:
    def test_normal_filled(self):
        r = exec_core.parse_execution_result(
            {'orderId': 42, 'status': 'FILLED', 'executedQty': '100',
             'cumQty': '100', 'avgPrice': '101.5'}, entry_price=100.0)
        assert r.status == 'FILLED'
        assert r.filled_qty == 100.0
        assert r.cum_qty == 100.0
        assert r.avg_price == 101.5
        assert r.order_id == 42
        assert not r.rejected   # 原始 truthy 值（None），非布尔 False

    def test_avg_price_zero_falls_back_to_entry(self):
        r = exec_core.parse_execution_result(
            {'orderId': 1, 'status': 'FILLED', 'executedQty': '100',
             'avgPrice': '0'}, 100.0)
        assert r.avg_price == 100.0

    def test_avg_price_missing_falls_back(self):
        r = exec_core.parse_execution_result(
            {'status': 'FILLED', 'executedQty': '100'}, 100.0)
        assert r.avg_price == 100.0

    def test_avg_price_empty_string_falls_back(self):
        r = exec_core.parse_execution_result(
            {'status': 'FILLED', 'executedQty': '100', 'avgPrice': ''}, 100.0)
        assert r.avg_price == 100.0

    def test_avg_price_negative_falls_back(self):
        r = exec_core.parse_execution_result(
            {'status': 'FILLED', 'executedQty': '100', 'avgPrice': '-1'}, 100.0)
        assert r.avg_price == 100.0

    def test_executed_qty_missing_zero(self):
        r = exec_core.parse_execution_result({'status': 'FILLED'}, 100.0)
        assert r.filled_qty == 0.0
        assert r.cum_qty == 0.0   # cumQty 缺失回退 executedQty

    def test_cum_qty_missing_falls_back_to_filled(self):
        r = exec_core.parse_execution_result(
            {'status': 'FILLED', 'executedQty': '30'}, 100.0)
        assert r.cum_qty == 30.0

    def test_status_missing_defaults_new(self):
        r = exec_core.parse_execution_result({'executedQty': '5'}, 100.0)
        assert r.status == 'NEW'

    def test_executed_qty_negative_uses_abs(self):
        r = exec_core.parse_execution_result(
            {'status': 'FILLED', 'executedQty': '-5', 'cumQty': '-5'}, 100.0)
        assert r.filled_qty == 5.0
        assert r.cum_qty == 5.0

    def test_order_id_missing_none(self):
        r = exec_core.parse_execution_result({'status': 'NEW'}, 100.0)
        assert r.order_id is None

    def test_non_numeric_executed_qty_raises_valueerror(self):
        """异常类型冻结：float('abc') → ValueError（真实路径 → 开仓 False）。"""
        with pytest.raises(ValueError):
            exec_core.parse_execution_result(
                {'status': 'FILLED', 'executedQty': 'abc'}, 100.0)

    def test_executed_qty_none_raises_typeerror(self):
        with pytest.raises(TypeError):
            exec_core.parse_execution_result(
                {'status': 'FILLED', 'executedQty': None}, 100.0)

    def test_avg_price_non_numeric_raises_valueerror(self):
        with pytest.raises(ValueError):
            exec_core.parse_execution_result(
                {'status': 'FILLED', 'executedQty': '1', 'avgPrice': 'abc'}, 100.0)

    @pytest.mark.parametrize('result,entry', [
        ({'orderId': 7, 'status': 'FILLED', 'executedQty': '100',
          'cumQty': '100', 'avgPrice': '101.5'}, 100.0),
        ({'orderId': 8, 'status': 'NEW', 'executedQty': '0',
          'cumQty': '0'}, 100.0),
        ({'status': 'FILLED', 'executedQty': '30', 'avgPrice': ''}, 50.0),
        ({'status': 'FILLED', 'executedQty': '-5'}, 2.0),
        ({'executedQty': '5'}, 100.0),
    ])
    def test_parity_reference(self, result, entry):
        got = exec_core.parse_execution_result(result, entry)
        ref = REF_parse(result, entry)
        assert (got.status, got.filled_qty, got.cum_qty, got.avg_price) == ref


# ═══════════════════════════════════════════════════════════════
#  Fill classification（真实阈值与分支顺序）
# ═══════════════════════════════════════════════════════════════

class TestClassifyOpenFill:
    def test_unfilled_new(self):
        assert exec_core.classify_open_fill('NEW', 0.0, 0.0, 100.0) \
            is exec_core.OpenFillOutcome.UNFILLED_NEW

    def test_unfilled_new_wins_over_zero_fill(self):
        """冻结分支顺序：NEW+0 优先于 zero-fill（即使 cum 也为 0）。"""
        assert exec_core.classify_open_fill('NEW', 0.0, 0.0, 100.0) \
            is exec_core.OpenFillOutcome.UNFILLED_NEW

    def test_zero_fill(self):
        assert exec_core.classify_open_fill('FILLED', 0.0, 0.0, 100.0) \
            is exec_core.OpenFillOutcome.ZERO_FILL

    def test_zero_fill_boundary_below(self):
        """0.005 < 0.01 → ZERO_FILL。"""
        assert exec_core.classify_open_fill('FILLED', 0.005, 0.005, 100.0) \
            is exec_core.OpenFillOutcome.ZERO_FILL

    def test_cum_nonzero_but_filled_zero_is_partial(self):
        """filled=0 但 cum=5 → 跳过 zero 分支 → PARTIAL（真实行为）。"""
        assert exec_core.classify_open_fill('FILLED', 0.0, 5.0, 100.0) \
            is exec_core.OpenFillOutcome.PARTIAL_BELOW_HALF

    def test_partial_below_half(self):
        assert exec_core.classify_open_fill('FILLED', 30.0, 30.0, 100.0) \
            is exec_core.OpenFillOutcome.PARTIAL_BELOW_HALF

    def test_partial_boundary_exactly_half_is_accepted(self):
        """真实阈值是严格小于：filled == qty*0.5 → ACCEPTED。"""
        assert exec_core.classify_open_fill('FILLED', 50.0, 50.0, 100.0) \
            is exec_core.OpenFillOutcome.ACCEPTED

    def test_partial_boundary_just_below(self):
        assert exec_core.classify_open_fill('FILLED', 49.9999, 49.9999, 100.0) \
            is exec_core.OpenFillOutcome.PARTIAL_BELOW_HALF

    def test_full_fill_accepted(self):
        assert exec_core.classify_open_fill('FILLED', 100.0, 100.0, 100.0) \
            is exec_core.OpenFillOutcome.ACCEPTED

    def test_overfill_accepted(self):
        assert exec_core.classify_open_fill('FILLED', 120.0, 120.0, 100.0) \
            is exec_core.OpenFillOutcome.ACCEPTED

    def test_zero_fill_boundary_exactly_01_is_partial(self):
        """filled == 0.01 → 不满足 <0.01 → PARTIAL（qty=100）。"""
        assert exec_core.classify_open_fill('FILLED', 0.01, 0.01, 100.0) \
            is exec_core.OpenFillOutcome.PARTIAL_BELOW_HALF

    @pytest.mark.parametrize('status,filled,cum,qty', [
        ('NEW', 0.0, 0.0, 100.0),
        ('FILLED', 0.0, 0.0, 100.0),
        ('FILLED', 0.005, 0.005, 100.0),
        ('FILLED', 0.01, 0.01, 100.0),
        ('FILLED', 30.0, 30.0, 100.0),
        ('FILLED', 50.0, 50.0, 100.0),
        ('FILLED', 100.0, 100.0, 100.0),
        ('PARTIALLY_FILLED', 20.0, 20.0, 100.0),
        ('NEW', 0.0, 5.0, 100.0),
    ])
    def test_parity_reference(self, status, filled, cum, qty):
        """REFERENCE：提取前真实 if 链的等价分类。"""
        if status == 'NEW' and filled == 0:
            ref = exec_core.OpenFillOutcome.UNFILLED_NEW
        elif filled < 0.01 and cum < 0.01:
            ref = exec_core.OpenFillOutcome.ZERO_FILL
        elif filled < qty * 0.5:
            ref = exec_core.OpenFillOutcome.PARTIAL_BELOW_HALF
        else:
            ref = exec_core.OpenFillOutcome.ACCEPTED
        assert exec_core.classify_open_fill(status, filled, cum, qty) is ref


# ═══════════════════════════════════════════════════════════════
#  Quantity normalization
# ═══════════════════════════════════════════════════════════════

class TestRoundQtyByLotStep:
    def test_step_001(self):
        assert exec_core.round_qty_by_lot_step(1.23456, 0.01) == 1.23

    def test_step_1_truncates_not_rounds(self):
        """冻结：向下截断（17508.9 → 17508.0，非四舍五入 17509）。"""
        assert exec_core.round_qty_by_lot_step(17508.9, 1.0) == 17508.0

    def test_step_0001(self):
        assert exec_core.round_qty_by_lot_step(0.123456, 0.001) == 0.123

    def test_zero_qty(self):
        assert exec_core.round_qty_by_lot_step(0, 0.01) == 0.0

    def test_tiny_qty_below_step_becomes_zero(self):
        assert exec_core.round_qty_by_lot_step(0.005, 0.01) == 0.0

    def test_integer_input_float_artifact_frozen(self):
        """冻结浮点伪影：100.0 % 0.01 = 0.009999999999997671 → 99.99（非 100.0）。

        与提取前真实代码逐字一致（REF parity 用例覆盖同一输入）。"""
        assert exec_core.round_qty_by_lot_step(100.0, 0.01) == 99.99

    def test_negative_qty_frozen(self):
        """冻结 Python 负数取模语义：-10.5 % 1 = 0.5 → -11.0。"""
        assert exec_core.round_qty_by_lot_step(-10.5, 1.0) == -11.0

    def test_step_10(self):
        assert exec_core.round_qty_by_lot_step(25.0, 10.0) == 20.0

    @pytest.mark.parametrize('qty,step', [
        (1.23456, 0.01), (17508.9, 1.0), (0.123456, 0.001),
        (0, 0.01), (0.005, 0.01), (-10.5, 1.0), (25.0, 10.0),
        (100.0, 0.01), (7.999, 0.1), (3.14159, 0.01),
    ])
    def test_parity_reference_kernel(self, qty, step):
        assert exec_core.round_qty_by_lot_step(qty, step) \
            == REF_se_round_qty_kernel(qty, step)

    def test_parity_se_round_qty_via_wrapper(self, monkeypatch):
        """wrapper 路由后 se._round_qty 输出 == core（同一 lot-step 语义）。"""
        from strategies import shared_executor as se
        info = {'symbols': [{'symbol': 'XUSDT', 'filters': [
            {'filterType': 'LOT_SIZE', 'stepSize': '0.01000000'}]}]}
        monkeypatch.setattr(se, 'fapi_get', lambda p, q=None: info)
        assert se._round_qty('XUSDT', 1.23456) \
            == exec_core.round_qty_by_lot_step(1.23456, 0.01)


class TestRoundQtyFromExchangeInfo:
    def test_found_lot_size(self):
        info = {'symbols': [{'symbol': 'XUSDT', 'filters': [
            {'filterType': 'PRICE_FILTER', 'tickSize': '0.001'},
            {'filterType': 'LOT_SIZE', 'stepSize': '0.01'}]}]}
        assert exec_core.round_qty_from_exchange_info(info, 'XUSDT', 1.23456) == 1.23

    def test_missing_symbol_returns_none(self):
        assert exec_core.round_qty_from_exchange_info(
            {'symbols': []}, 'XUSDT', 1.23456) is None

    def test_no_lot_size_filter_returns_none(self):
        info = {'symbols': [{'symbol': 'XUSDT', 'filters': [
            {'filterType': 'PRICE_FILTER', 'tickSize': '0.001'}]}]}
        assert exec_core.round_qty_from_exchange_info(
            info, 'XUSDT', 1.23456) is None

    def test_non_dict_info_returns_none(self):
        assert exec_core.round_qty_from_exchange_info(None, 'XUSDT', 1.0) is None
        assert exec_core.round_qty_from_exchange_info([], 'XUSDT', 1.0) is None

    def test_no_symbols_key_returns_none(self):
        assert exec_core.round_qty_from_exchange_info({}, 'XUSDT', 1.0) is None


class TestRoundQtyByPrecision:
    def test_precision_6(self):
        assert exec_core.round_qty_by_precision(1.23456789, 6) == 1.234568

    def test_precision_0(self):
        assert exec_core.round_qty_by_precision(10.7, 0) == 11.0

    def test_parity_pm_round_qty(self, close_env):
        """core kernel == pm._round_qty 真实输出（PM 未改，get_symbol_info=(6,6)）。"""
        pm = close_env['pm']
        assert pm._round_qty('AUSDT', 1.23456789) \
            == exec_core.round_qty_by_precision(1.23456789, 6)
        assert pm._round_qty('AUSDT', 10.0) \
            == exec_core.round_qty_by_precision(10.0, 6)


# ═══════════════════════════════════════════════════════════════
#  Close 纯数学（parity vs 未改动的 PM）
# ═══════════════════════════════════════════════════════════════

class TestCloseMath:
    def test_remaining_after_partial(self):
        assert exec_core.remaining_after_partial(10.0, 4.0) == 6.0

    def test_remaining_after_partial_rounding_4(self):
        assert exec_core.remaining_after_partial(10.0, 0.00004) == 10.0

    def test_remaining_negative_close_qty_inverts_frozen(self):
        """冻结 E-OBS-7：close_qty<0 → qty 放大（10-(-100)=110）。"""
        assert exec_core.remaining_after_partial(10.0, -100.0) == 110.0

    def test_partial_pnl_u_long_short(self):
        assert exec_core.partial_pnl_u('LONG', 1.0, 1.1, 5.0) == 0.5
        assert exec_core.partial_pnl_u('SHORT', 2.0, 1.9, 4.0) == 0.4

    def test_position_pnl_short(self):
        got = exec_core.position_pnl('SHORT', 2.0, 1.9, 10.0)
        assert got == REF_pm_pnl('SHORT', 2.0, 1.9, 10.0)
        assert got[1] == 1.0            # pnl_u 精确
        assert got[0] == pytest.approx(5.0)

    def test_position_pnl_long(self):
        got = exec_core.position_pnl('LONG', 2.0, 2.1, 10.0)
        assert got == REF_pm_pnl('LONG', 2.0, 2.1, 10.0)
        assert got[1] == 1.0
        assert got[0] == pytest.approx(5.0)

    def test_position_pnl_pct_not_rounded(self):
        """冻结：pnl_pct 不 round（真实代码只在日志中格式化）。"""
        pct, _ = exec_core.position_pnl('SHORT', 3.0, 2.987, 10.0)
        assert pct == (3.0 - 2.987) / 3.0 * 100

    @pytest.mark.parametrize('side,entry,price,qty', [
        ('SHORT', 2.0, 1.9, 10.0), ('LONG', 2.0, 2.1, 10.0),
        ('SHORT', 100.0, 97.5, 3.0), ('LONG', 1.0, 1.0001, 0.0),
        ('SHORT', 0.5, 0.4, 100.0),
    ])
    def test_parity_reference(self, side, entry, price, qty):
        assert exec_core.position_pnl(side, entry, price, qty) \
            == REF_pm_pnl(side, entry, price, qty)

    def test_parity_pm_close_realized_pnl(self, close_env):
        """core pnl_u == pm._close 真实 CLOSE_ORDER_FILLED realized_pnl。"""
        pm = close_env['pm']
        pos = _pos()
        positions = {'AUSDT': pos}
        assert pm._close('AUSDT', pos, 1.9, '硬止损', positions) is True
        pg = [e for e in close_env['calls']['pg']
              if e['event_type'] == 'CLOSE_ORDER_FILLED'][0]
        expect = exec_core.position_pnl('SHORT', 2.0, 1.9, 10.0)
        assert pg['realized_pnl'] == expect[1] == 1.0

    def test_has_remaining_position_threshold(self):
        """冻结阈值：>= 0.001 为剩余。"""
        assert exec_core.has_remaining_position(0.001) is True
        assert exec_core.has_remaining_position(0.0009) is False
        assert exec_core.has_remaining_position(0.0) is False

    def test_accounted_close_qty_fallback(self):
        assert exec_core.accounted_close_qty(4.0, 10.0) == 4.0
        assert exec_core.accounted_close_qty(0.0005, 10.0) == 10.0   # <0.001 → 请求量
        assert exec_core.accounted_close_qty(0.0, 10.0) == 10.0

    def test_parity_pm_close_accounted_qty_fallback(self, close_env):
        """executedQty 缺失 → 真实 PM 用请求量记账 == core fallback。"""
        pm = close_env['pm']
        close_env['set_risk_responses']([
            [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}], []])
        close_env['set_order_result']({'orderId': 1, 'status': 'FILLED'})  # 无 executedQty
        pos = _pos()
        positions = {'AUSDT': pos}
        assert pm._close('AUSDT', pos, 1.9, '硬止损', positions) is True
        pg = [e for e in close_env['calls']['pg']
              if e['event_type'] == 'CLOSE_ORDER_FILLED'][0]
        assert pg['qty'] == exec_core.accounted_close_qty(0.0, 10.0) == 10.0
        assert pg['realized_pnl'] == exec_core.position_pnl(
            'SHORT', 2.0, 1.9, 10.0)[1]

    def test_parity_pm_partial_remaining(self, close_env):
        """core remaining == pm._partial_close 真实剩余（PM 未改）。"""
        pm = close_env['pm']
        pos = _pos(side='LONG', entry=1.0, qty=10.0, original_qty=10.0)
        positions = {'AUSDT': pos}
        pm._partial_close('AUSDT', pos, 1.1, 5.0, 5, positions)
        assert pos['qty'] == exec_core.remaining_after_partial(10.0, 5.0) == 5.0

    def test_parity_pm_partial_negative_inversion(self, close_env):
        """OBS-7 反向放大 parity：真实 PM 与 core 行为一致（未修复）。"""
        pm = close_env['pm']
        pos = _pos(qty=10.0)
        positions = {'AUSDT': pos}
        pm._partial_close('AUSDT', pos, 1.1, -100.0, 5, positions)
        assert pos['qty'] == exec_core.remaining_after_partial(10.0, -100.0) == 110.0


# ═══════════════════════════════════════════════════════════════
#  Sandbox 拦截谓词
# ═══════════════════════════════════════════════════════════════

class TestIsOrderPath:
    @pytest.mark.parametrize('path,expected', [
        ('/fapi/v1/order', True),
        ('/fapi/v1/algoOrder', True),
        ('/fapi/v1/leverage', False),
        ('/fapi/v1/marginType', False),
        ('/fapi/v1/cancelOrder', True),
        ('', False),
    ])
    def test_predicate(self, path, expected):
        assert exec_core.is_order_path(path) is expected

    def test_parity_se_sandbox_post_routing(self, monkeypatch):
        """se._sandbox_post 经 core 谓词后行为不变（非 order 路径不拦截）。"""
        from strategies import shared_executor as se
        monkeypatch.setattr(se, '_sandbox_check', lambda: True)
        # 真实 _sandbox_post 跑真实 core 谓词：leverage 不含 'order' → None
        assert se._sandbox_post('/fapi/v1/leverage', {}) is None
        # order 路径会进 mock_post_order；这里只验证谓词分流已生效
        assert exec_core.is_order_path('/fapi/v1/order')


# ═══════════════════════════════════════════════════════════════
#  架构验证：stdlib only + 无 IO 传递依赖
# ═══════════════════════════════════════════════════════════════

class TestCoreDependencyRule:
    FORBIDDEN_SRC = ('redis', 'requests', 'binance', 'position_manager',
                     'shared_executor', 'strategies', 'psycopg', 'telegram',
                     'threading', 'queue', 'time', 'shared', 'socket', 'os')

    def test_core_source_has_no_forbidden_imports(self):
        src = inspect.getsource(exec_core)
        for mod in self.FORBIDDEN_SRC:
            assert not re.search(rf'^\s*(import|from)\s+{re.escape(mod)}\b',
                                 src, re.M), f'forbidden import: {mod}'

    def test_core_import_pulls_no_io_modules(self):
        """子进程干净 import execution.core → sys.modules 不得出现 IO 模块。"""
        repo = Path(__file__).resolve().parents[2]
        code = ("import sys; import execution.core; "
                "bad = [m for m in ('redis', 'requests', 'psycopg', 'telegram', "
                "'binance', 'shared.position_manager', 'strategies.shared_executor', "
                "'shared.redis_store') if m in sys.modules]; print(bad)")
        out = subprocess.run([sys.executable, '-c', code], capture_output=True,
                             text=True, cwd=str(repo), timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == '[]'

    def test_se_uses_same_core_module(self):
        """shared_executor 引用的 core 与测试引用的是同一模块对象。"""
        from strategies import shared_executor as se
        assert se._exec_core is exec_core
