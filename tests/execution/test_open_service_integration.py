"""P4-03-01-B：open_position → ExecutionService 集成测试。

验证迁移 behavior-preserving：
- open_position 真正经过 ExecutionService（工厂 spy）
- Port 收到的 intent 与 core 生成一致（对象/字段/params）
- SE Open 参数冻结（MARKET / RESULT / 无 positionSide / 无 reduceOnly）
- 成功/失败/PM 失败/duplicate 顺序/fill 分类/异常 全部保持 E-OBS
"""
import pytest

from execution import service as exec_service
from execution.adapters.binance import SharedExecutorBinanceAdapter
from execution.core import se_open_intent


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


FILLED_100 = {'orderId': 1, 'status': 'FILLED', 'executedQty': '100',
              'cumQty': '100', 'avgPrice': '100.0'}


def _make_open_kwargs(**kw):
    base = {'name': 'S6', 'symbol': 'TESTUSDT', 'side': 'LONG',
            'entry_price': 100.0, 'stop_price': 95.0, 'qty': 100.0,
            'margin_mode': 'CROSSED', 'leverage': 3,
            'event_type': 'TREND_UP', 'strength': 70, 'tg_fn': None,
            'expected_move_pct': 0, 'decision_context': {}}
    base.update(kw)
    return base


def _order_posts(calls):
    return [p for p in calls['post'] if p['path'] == '/fapi/v1/order']


def _factory_spy(exec_env, monkeypatch):
    """包装 se._execution_service：记录调用次数，内部仍走真实 adapter。"""
    se = exec_env['se']
    hits = []
    orig = se._execution_service

    def spy():
        hits.append(1)
        return orig()
    monkeypatch.setattr(se, '_execution_service', spy)
    return hits


# ═══════════════════════════════════════════════════════════════
#  1. open_position 确实通过 ExecutionService
# ═══════════════════════════════════════════════════════════════

def test_open_position_routes_through_service(exec_env, monkeypatch):
    hits = _factory_spy(exec_env, monkeypatch)
    se = exec_env['se']
    r = se.open_position(**_make_open_kwargs())
    assert r is True
    assert hits == [1]                      # 每次开仓恰好构建/使用一次 service
    assert len(_order_posts(exec_env['calls'])) == 1   # 经真实 adapter 下单一次


def test_factory_builds_se_semantics_adapter(exec_env):
    """工厂绑定 SharedExecutorBinanceAdapter（se.fapi_post 语义：异常→None）。"""
    svc = exec_env['se']._execution_service()
    assert isinstance(svc._binance, SharedExecutorBinanceAdapter)
    assert SharedExecutorBinanceAdapter.ORDER_PATH == '/fapi/v1/order'


# ═══════════════════════════════════════════════════════════════
#  2/3. Service 收到的 intent 与 core 生成一致
# ═══════════════════════════════════════════════════════════════

def test_service_receives_core_intent(exec_env, monkeypatch):
    se = exec_env['se']
    spy = SpyBinancePort(result=FILLED_100)
    monkeypatch.setattr(se, '_execution_service',
                        lambda: exec_service.ExecutionService(binance=spy))
    r = se.open_position(**_make_open_kwargs())
    assert r is True
    assert len(spy.received) == 1
    expect = se_open_intent('TESTUSDT', 'LONG', 100.0)   # gates 不改 qty（100）
    assert spy.received[0] == expect                     # dataclass 字段级相等
    assert spy.received[0].to_params() == expect.to_params()
    assert spy.received[0].newOrderRespType == 'RESULT'
    assert spy.received[0].positionSide is None
    assert spy.received[0].reduceOnly is None


# ═══════════════════════════════════════════════════════════════
#  4. SE Open 参数冻结（经真实 adapter 路径）
# ═══════════════════════════════════════════════════════════════

def test_se_open_params_preserved_through_service(exec_env, monkeypatch):
    _factory_spy(exec_env, monkeypatch)
    exec_env['se'].open_position(**_make_open_kwargs(side='SHORT'))
    order = _order_posts(exec_env['calls'])[0]['params']
    assert order == {'symbol': 'TESTUSDT', 'side': 'SELL', 'type': 'MARKET',
                     'quantity': 100.0, 'newOrderRespType': 'RESULT'}
    assert 'positionSide' not in order      # 不改成 BOTH
    assert 'reduceOnly' not in order        # 不添加 reduceOnly


# ═══════════════════════════════════════════════════════════════
#  5. Binance 成功 → 原 PM update / PG / TG / Algo 流程继续
# ═══════════════════════════════════════════════════════════════

def test_success_continues_pm_pg_tg_algo(exec_env, monkeypatch):
    _factory_spy(exec_env, monkeypatch)
    se = exec_env['se']
    r = se.open_position(**_make_open_kwargs(tg_fn=exec_env['calls']['tg'].append))
    assert r is True
    stored = exec_env['redis'].get('pm:positions')['TESTUSDT']
    assert stored['entry'] == 100.0 and stored['qty'] == 100.0   # PM update 原样
    assert [e['event_type'] for e in exec_env['calls']['pg']] == ['OPEN_ORDER_FILLED']
    assert len(exec_env['calls']['tg']) == 1
    assert exec_env['calls']['algo'] == [{'symbol': 'TESTUSDT', 'side': 'SELL',
                                          'trigger': 95.0, 'qty': 100.0}]


# ═══════════════════════════════════════════════════════════════
#  6. Binance failure → 原 failure behavior
# ═══════════════════════════════════════════════════════════════

def test_binance_reject_keeps_failure_behavior(exec_env, monkeypatch):
    _factory_spy(exec_env, monkeypatch)
    exec_env['ctrl']['order_result'] = {'code': -2019, 'msg': 'insufficient'}
    r = exec_env['se'].open_position(**_make_open_kwargs())
    assert r is False
    assert len(_order_posts(exec_env['calls'])) == 1    # 单次尝试（无 retry）
    assert exec_env['redis'].get('pm:positions') is None
    assert exec_env['calls']['pg'] == []
    assert exec_env['calls']['algo'] == []


def test_binance_none_result_keeps_failure_behavior(exec_env, monkeypatch):
    """fapi_post 异常→None 语义经 service 透传 → 原路径 return False。"""
    _factory_spy(exec_env, monkeypatch)
    se = exec_env['se']
    base_post = se.fapi_post

    def none_for_order(path, params=None):
        if 'order' in path:
            exec_env['calls']['post'].append({'path': path, 'params': params})
            return None
        return base_post(path, params)
    monkeypatch.setattr(se, 'fapi_post', none_for_order)
    r = se.open_position(**_make_open_kwargs())
    assert r is False
    assert len(_order_posts(exec_env['calls'])) == 1
    assert exec_env['redis'].get('pm:positions') is None


def test_port_exception_returns_false_via_outer_handler(exec_env, monkeypatch):
    """Port 异常 → service 原样上抛 → open_position 外层 try → False（无 retry）。"""
    se = exec_env['se']
    spy = SpyBinancePort(exc=RuntimeError('network down'))
    monkeypatch.setattr(se, '_execution_service',
                        lambda: exec_service.ExecutionService(binance=spy))
    r = se.open_position(**_make_open_kwargs())
    assert r is False
    assert len(spy.received) == 1
    assert exec_env['calls']['pg'] == []
    assert exec_env['redis'].get('pm:positions') is None


# ═══════════════════════════════════════════════════════════════
#  7. PM failure → cancel attempt + False（E-OBS-1 保持）
# ═══════════════════════════════════════════════════════════════

def test_pm_failure_keeps_cancel_attempt(exec_env, monkeypatch):
    _factory_spy(exec_env, monkeypatch)
    se = exec_env['se']
    exec_env['ctrl']['order_result'] = {'orderId': 42, 'status': 'FILLED',
                                        'executedQty': '100', 'avgPrice': '100.0'}

    def _failing_update(*a, **k):
        raise RuntimeError('cache update failed')
    monkeypatch.setattr(se, '_update_pos_cache', _failing_update)
    r = se.open_position(**_make_open_kwargs())
    assert r is False
    # 经 service 下单一次成功 → PM 失败 → 取消订单（原行为）
    assert len(_order_posts(exec_env['calls'])) == 1
    assert exec_env['calls']['cancel'] == [{'symbol': 'TESTUSDT', 'orderId': 42}]
    assert exec_env['calls']['pg'] == []


# ═══════════════════════════════════════════════════════════════
#  8. duplicate check 顺序不变（E-OBS-2：不在 service 前置/新增）
# ═══════════════════════════════════════════════════════════════

def test_existing_position_gate_runs_before_service(exec_env, monkeypatch):
    """交易所已有仓 gate（原 G9）在 service 之前 → service 未被调用。"""
    hits = _factory_spy(exec_env, monkeypatch)
    exec_env['ctrl']['existing_position'] = [
        {'symbol': 'TESTUSDT', 'positionAmt': '5'}]
    r = exec_env['se'].open_position(**_make_open_kwargs())
    assert r is False
    assert hits == []                       # service 从未触达
    assert _order_posts(exec_env['calls']) == []


def test_no_new_duplicate_check_between_service_and_pm(exec_env, monkeypatch):
    """order 先行 → PM 注册在后；service 与 PM 之间未新增任何 check。"""
    hits = _factory_spy(exec_env, monkeypatch)
    r = exec_env['se'].open_position(**_make_open_kwargs())
    assert r is True
    assert hits == [1]
    assert len(_order_posts(exec_env['calls'])) == 1
    assert 'TESTUSDT' in exec_env['redis'].get('pm:positions')


# ═══════════════════════════════════════════════════════════════
#  9/10. fill 分类阈值不变
# ═══════════════════════════════════════════════════════════════

def test_fill_60pct_accepted_no_cancel(exec_env, monkeypatch):
    _factory_spy(exec_env, monkeypatch)
    exec_env['ctrl']['order_result'] = {'orderId': 42, 'status': 'FILLED',
                                        'executedQty': '60', 'cumQty': '60',
                                        'avgPrice': '100.0'}
    r = exec_env['se'].open_position(**_make_open_kwargs())
    assert r is True
    assert exec_env['calls']['cancel'] == []            # ≥50% → accepted
    stored = exec_env['redis'].get('pm:positions')['TESTUSDT']
    assert stored['qty'] == 60.0
    assert exec_env['calls']['algo'][0]['qty'] == 60.0


def test_fill_30pct_cancels_remaining(exec_env, monkeypatch):
    _factory_spy(exec_env, monkeypatch)
    exec_env['ctrl']['order_result'] = {'orderId': 42, 'status': 'FILLED',
                                        'executedQty': '30', 'cumQty': '30',
                                        'avgPrice': '100.0'}
    r = exec_env['se'].open_position(**_make_open_kwargs())
    assert r is True
    assert exec_env['calls']['cancel'] == [{'symbol': 'TESTUSDT', 'orderId': 42}]
    stored = exec_env['redis'].get('pm:positions')['TESTUSDT']
    assert stored['qty'] == 30.0            # <50% → cancel 剩余，接受已成交
