"""P4-03-01-C：Full Close / Partial Close → ExecutionService 集成测试。

验证迁移 behavior-preserving（20 项覆盖）：
- close/partial 真正经过 ExecutionService（工厂 spy / SpyBinancePort）
- intent == core close_intent / partial_close_intent 生成（字段/params 逐字）
- LONG close→SELL / SHORT close→BUY；MARKET / BOTH / reduceOnly 差异冻结
- close_qty 原样传递（含负数，无 clamp——E-OBS-7）
- 异常/拒绝/already-flat/PM 后续流程/closed marker 时序 全部保持
"""
import pytest

from execution import service as exec_service
from execution.core import close_intent, partial_close_intent


class SpyBinancePort:
    """Service 层 spy：捕获 execute_order → place_order 的 intent。"""

    def __init__(self, result=None, exc=None):
        self.received = []
        self.result = result
        self.exc = exc

    def place_order(self, intent):
        self.received.append(intent)
        if self.exc is not None:
            raise self.exc
        return self.result


CLOSED_FILLED = {'orderId': 1, 'status': 'FILLED', 'executedQty': '10'}
PARTIAL_OK = {'orderId': 2, 'status': 'FILLED'}


def _pos(**kw):
    base = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
            'system': 'S8', 'open_time': 1788000000, 'leverage': 3,
            'sl': 2.2, 'signal_type': 'TREND_DOWN', 'event_type': 'TREND_DOWN',
            'score': 66, 'be_done': False}
    base.update(kw)
    return base


def _order_posts(calls):
    return [p for p in calls['post'] if 'order' in p['path']]


def _factory_spy(close_env, monkeypatch):
    """包装 pm._execution_service：记录调用次数，内部仍走真实 adapter
    （adapter 绑定 close_env 已 patch 的 _s6api fapi_post → 记录到 calls['post']）。"""
    pm = close_env['pm']
    hits = []
    orig = pm._execution_service

    def spy():
        hits.append(1)
        return orig()
    monkeypatch.setattr(pm, '_execution_service', spy)
    return hits


def _spy_service(close_env, monkeypatch, result=None, exc=None):
    """替换 pm._execution_service 为 SpyBinancePort 版本（绕过真实 adapter）。"""
    pm = close_env['pm']
    spy = SpyBinancePort(result=result, exc=exc)
    monkeypatch.setattr(pm, '_execution_service',
                        lambda: exec_service.ExecutionService(binance=spy))
    return spy


# ═══════════════════════════════════════════════════════════════
#  Full Close（1-8）
# ═══════════════════════════════════════════════════════════════

def test_full_close_routes_through_service(close_env, monkeypatch):
    hits = _factory_spy(close_env, monkeypatch)
    pos = _pos()
    positions = {'AUSDT': pos}
    r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
    assert r is True
    assert hits == [1]                       # 经 service 恰好一次
    assert len(_order_posts(close_env['calls'])) == 1   # 经真实 adapter 下单
    assert 'AUSDT' not in positions


def test_service_receives_close_intent(close_env, monkeypatch):
    spy = _spy_service(close_env, monkeypatch, result=CLOSED_FILLED)
    close_env['set_risk_responses']([
        [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}], []])
    pos = _pos()
    positions = {'AUSDT': pos}
    r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
    assert r is True
    assert len(spy.received) == 1
    expect = close_intent('AUSDT', 'SHORT', 10.0)   # abs(positionAmt)=10
    assert spy.received[0] == expect                # dataclass 字段级相等
    assert spy.received[0].quantity == 10.0
    assert spy.received[0].type == 'MARKET'
    assert spy.received[0].positionSide == 'BOTH'
    assert spy.received[0].reduceOnly == 'true'
    assert spy.received[0].newOrderRespType is None


def test_long_close_sells(close_env, monkeypatch):
    spy = _spy_service(close_env, monkeypatch, result=CLOSED_FILLED)
    close_env['set_risk_responses']([
        [{'symbol': 'AUSDT', 'positionAmt': '10', 'entryPrice': '2.0'}], []])
    pos = _pos(side='LONG')
    positions = {'AUSDT': pos}
    assert close_env['pm']._close('AUSDT', pos, 2.1, '手动平仓', positions) is True
    assert spy.received[0].side == 'SELL'


def test_short_close_buys(close_env, monkeypatch):
    spy = _spy_service(close_env, monkeypatch, result=CLOSED_FILLED)
    close_env['set_risk_responses']([
        [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}], []])
    pos = _pos()
    positions = {'AUSDT': pos}
    assert close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions) is True
    assert spy.received[0].side == 'BUY'


def test_full_close_params_exact(close_env, monkeypatch):
    """5-8：MARKET / BOTH / reduceOnly='true' / 无额外参数（经真实 adapter 捕获）。"""
    _factory_spy(close_env, monkeypatch)
    pos = _pos()
    positions = {'AUSDT': pos}
    assert close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions) is True
    order = _order_posts(close_env['calls'])[0]['params']
    assert order == {'symbol': 'AUSDT', 'side': 'BUY', 'type': 'MARKET',
                     'quantity': 10.0, 'positionSide': 'BOTH',
                     'reduceOnly': 'true'}
    assert list(order.keys()) == ['symbol', 'side', 'type', 'quantity',
                                  'positionSide', 'reduceOnly']


# ═══════════════════════════════════════════════════════════════
#  Partial Close（9-15）
# ═══════════════════════════════════════════════════════════════

def test_partial_routes_through_service(close_env, monkeypatch):
    hits = _factory_spy(close_env, monkeypatch)
    pos = _pos()
    positions = {'AUSDT': pos}
    close_env['pm']._partial_close('AUSDT', pos, 1.9, 4.0, 5, positions)
    assert hits == [1]                       # 经 service 恰好一次
    assert len(_order_posts(close_env['calls'])) == 1
    assert pos['qty'] == 6.0


def test_service_receives_partial_close_intent(close_env, monkeypatch):
    spy = _spy_service(close_env, monkeypatch, result=PARTIAL_OK)
    pos = _pos()
    positions = {'AUSDT': pos}
    close_env['pm']._partial_close('AUSDT', pos, 1.9, 4.0, 5, positions)
    assert len(spy.received) == 1
    expect = partial_close_intent('AUSDT', 'SHORT', 4.0)
    assert spy.received[0] == expect
    assert spy.received[0].quantity == 4.0   # qty 原样传递
    assert spy.received[0].reduceOnly is None
    assert spy.received[0].newOrderRespType is None


def test_long_partial_sells(close_env, monkeypatch):
    spy = _spy_service(close_env, monkeypatch, result=PARTIAL_OK)
    pos = _pos(side='LONG', entry=1.0)
    positions = {'AUSDT': pos}
    close_env['pm']._partial_close('AUSDT', pos, 1.1, 5.0, 5, positions)
    assert spy.received[0].side == 'SELL'


def test_short_partial_buys(close_env, monkeypatch):
    spy = _spy_service(close_env, monkeypatch, result=PARTIAL_OK)
    pos = _pos()
    positions = {'AUSDT': pos}
    close_env['pm']._partial_close('AUSDT', pos, 1.9, 4.0, 5, positions)
    assert spy.received[0].side == 'BUY'


def test_partial_params_exact(close_env, monkeypatch):
    """12-14：MARKET / BOTH / 无 reduceOnly / 无额外参数（经真实 adapter 捕获）。"""
    _factory_spy(close_env, monkeypatch)
    pos = _pos(side='LONG', entry=1.0)
    positions = {'AUSDT': pos}
    close_env['pm']._partial_close('AUSDT', pos, 1.1, 5.0, 5, positions)
    order = _order_posts(close_env['calls'])[0]['params']
    assert order == {'symbol': 'AUSDT', 'side': 'SELL', 'type': 'MARKET',
                     'quantity': 5.0, 'positionSide': 'BOTH'}
    assert list(order.keys()) == ['symbol', 'side', 'type', 'quantity',
                                  'positionSide']


def test_partial_negative_qty_passthrough_no_clamp(close_env, monkeypatch):
    """E-OBS-7 冻结：负 close_qty 原样进 intent（无 abs/clamp/reject），
    qty 反向放大行为经 service 后不变。"""
    spy = _spy_service(close_env, monkeypatch, result=PARTIAL_OK)
    pos = _pos()
    positions = {'AUSDT': pos}
    close_env['pm']._partial_close('AUSDT', pos, 1.1, -100.0, 5, positions)
    assert spy.received[0].quantity == -100.0
    assert pos['qty'] == 110.0


# ═══════════════════════════════════════════════════════════════
#  Failure（16-18）
# ═══════════════════════════════════════════════════════════════

def test_close_binance_exception_old_behavior(close_env, monkeypatch):
    """Port 异常 → _close 原路径：'[平仓异常]' + 清标记 + False + 保留仓位。"""
    _spy_service(close_env, monkeypatch, exc=RuntimeError('network down'))
    pos = _pos()
    positions = {'AUSDT': pos}
    r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
    assert r is False
    assert 'AUSDT' in positions              # 仓位保留
    assert close_env['redis'].get('closed:AUSDT') is None  # 标记被清除
    assert len(close_env['calls']['record']) == 0


def test_close_rejection_old_behavior(close_env, monkeypatch):
    """拒绝响应 → 原路径：清标记 + False + 保留仓位。"""
    _spy_service(close_env, monkeypatch,
                 result={'code': -2019, 'msg': 'Margin is insufficient.'})
    pos = _pos()
    positions = {'AUSDT': pos}
    r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
    assert r is False
    assert 'AUSDT' in positions
    assert close_env['redis'].get('closed:AUSDT') is None


def test_partial_binance_exception_old_behavior(close_env, monkeypatch):
    """Port 异常 → partial 原路径：log + return，qty 不变。"""
    _spy_service(close_env, monkeypatch, exc=RuntimeError('network down'))
    pos = _pos()
    positions = {'AUSDT': pos}
    close_env['pm']._partial_close('AUSDT', pos, 1.9, 4.0, 5, positions)
    assert pos['qty'] == 10.0                # 不变


def test_partial_rejection_old_behavior(close_env, monkeypatch):
    """拒绝响应 → partial 原路径：qty 不变。"""
    _spy_service(close_env, monkeypatch,
                 result={'code': -1111, 'msg': 'Precision error'})
    pos = _pos()
    positions = {'AUSDT': pos}
    close_env['pm']._partial_close('AUSDT', pos, 1.9, 4.0, 5, positions)
    assert pos['qty'] == 10.0


def test_already_flat_no_service_order(close_env, monkeypatch):
    """already-flat：无订单（port 未被调用）、EXCHANGE_POSITION_FLAT、True。"""
    spy = _spy_service(close_env, monkeypatch, result=CLOSED_FILLED)
    close_env['set_risk_responses']([[]])    # 首次查仓即空
    pos = _pos()
    positions = {'AUSDT': pos}
    r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
    assert r is True
    assert spy.received == []                # already-flat 路径不发订单
    pg_types = [e['event_type'] for e in close_env['calls']['pg']]
    assert pg_types == ['EXCHANGE_POSITION_FLAT']


# ═══════════════════════════════════════════════════════════════
#  19/20：PM 后续流程 + closed marker 时序
# ═══════════════════════════════════════════════════════════════

def test_pm_flow_continues_after_service_success(close_env, monkeypatch):
    """service 成功后：record(final=True) + pg(CLOSE_ORDER_FILLED) + pop + save。"""
    _spy_service(close_env, monkeypatch, result=CLOSED_FILLED)
    close_env['set_risk_responses']([
        [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}], []])
    pos = _pos()
    positions = {'AUSDT': pos}
    assert close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions) is True
    rec = close_env['calls']['record'][0]
    assert rec['kwargs']['final_close'] is True
    assert rec['kwargs']['exit_reason'] == '硬止损'
    pg = [e for e in close_env['calls']['pg']
          if e['event_type'] == 'CLOSE_ORDER_FILLED'][0]
    assert pg['realized_pnl'] == 1.0         # (2.0-1.9)*10（SHORT）
    assert 'AUSDT' not in positions


def test_closed_marker_sequence_preserved(close_env, monkeypatch):
    """时序冻结：mark → risk#1 → [service order] → risk#2 → cancel → pg → record → save。"""
    pm = close_env['pm']
    events = []
    close_env['set_risk_responses']([
        [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}], []])

    class EventSpyPort:
        """把下单事件写入 events 时序的 spy port。"""

        def __init__(self):
            self.received = []

        def place_order(self, intent):
            self.received.append(intent)
            events.append('port_place_order')
            return CLOSED_FILLED

    spy = EventSpyPort()
    monkeypatch.setattr(pm, '_execution_service',
                        lambda: exec_service.ExecutionService(binance=spy))
    raw_get = close_env['fapi_get']
    risk_n = {'n': 0}

    def spy_get(path, params=None):
        r = raw_get(path, params)
        if 'positionRisk' in path:
            risk_n['n'] += 1
            events.append(f'positionRisk#{risk_n["n"]}')
        return r
    monkeypatch.setattr(pm, '_s6api', lambda: (
        spy_get, close_env['fapi_post'], lambda *a, **k: {},
        lambda sym: 2.0, lambda sym: (6, 6),
        lambda *a, **k: (0, 0, 0), lambda *a, **k: 50.0,
        lambda *a, **kw: events.append(
            ('record_trade', kw.get('final_close')))))
    monkeypatch.setattr(pm, '_mark_closed', lambda sym: events.append('mark_closed'))
    monkeypatch.setattr(pm, '_pg_record_event',
                        lambda e: events.append(('pg_record', e['event_type'])))
    monkeypatch.setattr(pm, '_cancel_all_algo', lambda sym: events.append('cancel_algo'))
    monkeypatch.setattr(pm, '_save', lambda positions: events.append('save'))

    pos = _pos()
    positions = {'AUSDT': pos}
    r = pm._close('AUSDT', pos, 1.9, '硬止损', positions)
    assert r is True
    assert events == [
        'mark_closed',                        # 先标记（E-OBS-6 时序不变）
        'positionRisk#1',
        'port_place_order',                   # 经 service/port 下单（位置与原 order 相同）
        'positionRisk#2',
        'cancel_algo',
        ('pg_record', 'CLOSE_ORDER_FILLED'),
        ('record_trade', True),
        'save',
    ]
    assert len(spy.received) == 1
