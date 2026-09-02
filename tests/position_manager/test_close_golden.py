"""Golden：PM 平仓（_close 沙盘/交易所已平/市价成功/失败/部分成交路径）。"""
import time

from pm_golden_helpers import (
    make_position,
    recorded_entry,
    recorded_exit_reason,
    recorded_price,
    recorded_qty,
    recorded_side,
    recorded_signal_type,
    recorded_symbol,
    seed_positions,
)


def _seed_one(pm_full, symbol='AUSDT', **overrides):
    pos = make_position(**overrides)
    seed_positions(pm_full['redis'], {symbol: pos})
    return pos


# ── 沙盘路径 ─────────────────────────────────────────────────────────────

def test_sandbox_close_long_records_and_removes(pm_full):
    """沙盘平多：记录 trade（含 side/reason/signal_type），持仓从 positions 移除。"""
    pm_full['set_sandbox'](True)
    pos = _seed_one(pm_full, entry=1.0, qty=100.0, original_qty=100.0,
                    system='S6', signal_type='TREND_UP')
    positions = {'AUSDT': pos}
    ok = pm_full['pm']._close('AUSDT', pos, 1.1, '时间止损', positions)
    assert ok is True
    assert 'AUSDT' not in positions
    assert len(pm_full['calls']['record_trade']) == 1
    rec = pm_full['calls']['record_trade'][0]
    assert recorded_symbol(rec) == 'AUSDT'
    assert recorded_entry(rec) == 1.0
    assert recorded_price(rec) == 1.1
    assert recorded_qty(rec) == 100.0                  # 用 original_qty 全平
    assert recorded_side(rec) == 'LONG'
    assert recorded_exit_reason(rec) == '时间止损'
    # Observed Current Behavior：_close 读取 pos['event_type']，而
    # PM.open_position 写入的字段名是 signal_type → PM 直开仓位的
    # recorded signal_type 恒为 ''（生产中 S6/S8 经 shared_executor 写 event_type）。
    assert recorded_signal_type(rec) == ''
    assert rec['kwargs']['final_close'] is True
    assert pm_full['calls']['pg'] == []                # 沙盘路径不写 pg 事件
    assert pm_full['redis'].get('pm:positions') == {} or \
        'AUSDT' not in (pm_full['redis'].get('pm:positions') or {})


def test_sandbox_close_short_pnl_direction(pm_full):
    """沙盘平空：entry=100 → price=90 为盈利方向（record 参数冻结）。"""
    pm_full['set_sandbox'](True)
    pos = _seed_one(pm_full, entry=100.0, qty=10.0, original_qty=10.0, side='SHORT')
    positions = {'AUSDT': pos}
    ok = pm_full['pm']._close('AUSDT', pos, 90.0, '手动平仓', positions)
    assert ok is True
    rec = pm_full['calls']['record_trade'][0]
    assert recorded_entry(rec) == 100.0 and recorded_price(rec) == 90.0
    assert recorded_qty(rec) == 10.0 and recorded_side(rec) == 'SHORT'
    # Observed Current Behavior：沙盘平仓路径不日志化 pnl（pnl_u 计算后未使用），
    # pnl 只能从 record 参数（entry/price/qty）推导。


def test_close_recently_closed_skips_without_side_effects(pm_full):
    """防重入：4h 内已处理 → 返回 False，不下单、不记录、不重复标记。"""
    pm_full['set_sandbox'](True)
    pos = _seed_one(pm_full)
    positions = {'AUSDT': pos}
    pm_full['redis'].set('closed:AUSDT', {'ts': time.time()})
    ok = pm_full['pm']._close('AUSDT', pos, 1.1, '硬止损', positions)
    assert ok is False
    assert pm_full['calls']['post'] == []
    assert pm_full['calls']['record_trade'] == []
    assert 'AUSDT' in positions                        # 持仓未被移除


def test_close_force_bypasses_recently_closed(pm_full):
    """force=True 绕过防重入标记，正常平仓。"""
    pm_full['set_sandbox'](True)
    pos = _seed_one(pm_full)
    positions = {'AUSDT': pos}
    pm_full['redis'].set('closed:AUSDT', {'ts': time.time()})
    ok = pm_full['pm']._close('AUSDT', pos, 1.1, '手动平仓', positions, force=True)
    assert ok is True
    assert 'AUSDT' not in positions


# ── 实盘路径 ─────────────────────────────────────────────────────────────

def test_close_exchange_flat_records_and_removes(pm_full):
    """交易所已无持仓（止损单在交易所触发）→ 记录 trade + pg 事件 + 移除。"""
    pm_full['set_sandbox'](False)
    pos = _seed_one(pm_full, entry=1.0, qty=100.0, original_qty=100.0,
                    side='SHORT', system='S8', signal_type='TREND_DOWN')
    positions = {'AUSDT': pos}
    # positionRisk 查询返回空 → 交易所已平
    pm_full['set_s6api'](fapi_get=lambda path, params=None: [],
                         record_trade=lambda *a, **kw: pm_full['calls']['record_trade'].append(
                             {'args': a, 'kwargs': kw}))
    ok = pm_full['pm']._close('AUSDT', pos, 0.95, '硬止损', positions)

    assert ok is True
    assert 'AUSDT' not in positions
    assert len(pm_full['calls']['record_trade']) == 1
    rec = pm_full['calls']['record_trade'][0]
    assert recorded_exit_reason(rec) == '硬止损'
    assert recorded_side(rec) == 'SHORT'
    assert rec['kwargs']['final_close'] is True
    pg_types = [e['event_type'] for e in pm_full['calls']['pg']]
    assert 'EXCHANGE_POSITION_FLAT' in pg_types
    assert pm_full['calls']['cancel_algo'] == ['AUSDT']


def test_close_market_order_success_removes_and_records(pm_full):
    """市价平仓成功：读取持仓 → 下 reduceOnly 单 → 确认已平 → 记录 + 移除。"""
    pm_full['set_sandbox'](False)
    pos = _seed_one(pm_full, entry=2.0, qty=10.0, original_qty=10.0, side='SHORT')
    positions = {'AUSDT': pos}

    risk_reads = {'n': 0}

    def fake_fapi_get(path, params=None):
        risk_reads['n'] += 1
        if risk_reads['n'] == 1:
            return [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}]
        return []                                          # 平仓后确认已平

    def fake_fapi_post(path, params=None):
        pm_full['calls']['post'].append({'path': path, 'params': params})
        return {'orderId': 77, 'status': 'FILLED', 'executedQty': '10'}

    pm_full['set_s6api'](fapi_get=fake_fapi_get, fapi_post=fake_fapi_post,
                         record_trade=lambda *a, **kw: pm_full['calls']['record_trade'].append(
                             {'args': a, 'kwargs': kw}))
    ok = pm_full['pm']._close('AUSDT', pos, 2.1, '手动平仓', positions)

    assert ok is True
    assert 'AUSDT' not in positions
    order = pm_full['calls']['post'][0]
    assert order['path'] == '/fapi/v1/order'
    assert order['params']['side'] == 'BUY'                # 平空用 BUY
    assert order['params']['reduceOnly'] == 'true'
    assert order['params']['quantity'] == 10.0
    rec = pm_full['calls']['record_trade'][0]
    assert recorded_qty(rec) == 10.0
    assert rec['kwargs']['final_close'] is True
    pg_types = [e['event_type'] for e in pm_full['calls']['pg']]
    assert 'CLOSE_ORDER_FILLED' in pg_types


def test_close_post_rejected_clears_marker_returns_false(pm_full):
    """交易所拒绝平仓单 → 返回 False、清除关闭标记（防裸奔）、持仓保留。"""
    pm_full['set_sandbox'](False)
    pos = _seed_one(pm_full, side='SHORT', qty=10.0, original_qty=10.0)
    positions = {'AUSDT': pos}

    def fake_fapi_get(path, params=None):
        return [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}]

    def fake_fapi_post(path, params=None):
        return {'code': -2019, 'msg': 'Margin is insufficient.'}

    pm_full['set_s6api'](fapi_get=fake_fapi_get, fapi_post=fake_fapi_post)
    ok = pm_full['pm']._close('AUSDT', pos, 2.1, '硬止损', positions)

    assert ok is False
    assert 'AUSDT' in positions                        # 持仓保留
    assert pm_full['calls']['record_trade'] == []      # 不记账
    assert pm_full['redis'].get('closed:AUSDT') is None  # 标记已清除


def test_close_partial_fill_keeps_remaining_quantity(pm_full):
    """部分成交：已成交部分记账（final_close=False），剩余数量保留在持仓。"""
    pm_full['set_sandbox'](False)
    pos = _seed_one(pm_full, entry=2.0, qty=10.0, original_qty=10.0, side='SHORT')
    positions = {'AUSDT': pos}

    risk_reads = {'n': 0}

    def fake_fapi_get(path, params=None):
        risk_reads['n'] += 1
        if risk_reads['n'] == 1:
            return [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}]
        return [{'symbol': 'AUSDT', 'positionAmt': '-6', 'entryPrice': '2.0'}]  # 剩余 6

    def fake_fapi_post(path, params=None):
        return {'orderId': 88, 'status': 'FILLED', 'executedQty': '4'}

    pm_full['set_s6api'](fapi_get=fake_fapi_get, fapi_post=fake_fapi_post,
                         record_trade=lambda *a, **kw: pm_full['calls']['record_trade'].append(
                             {'args': a, 'kwargs': kw}))
    ok = pm_full['pm']._close('AUSDT', pos, 2.1, '硬止损', positions)

    assert ok is False                                  # 未完全平仓
    assert positions['AUSDT']['qty'] == 6.0             # 剩余数量回写
    assert len(pm_full['calls']['record_trade']) == 1
    rec = pm_full['calls']['record_trade'][0]
    assert recorded_qty(rec) == 4.0
    assert rec['kwargs']['final_close'] is False
    pg_types = [e['event_type'] for e in pm_full['calls']['pg']]
    assert 'CLOSE_ORDER_PARTIAL' in pg_types


def test_close_removes_only_target_symbol(pm_full):
    """多持仓：平 A 不影响 B（positions 与 Redis 双侧）。"""
    pm_full['set_sandbox'](True)
    pos_a = _seed_one(pm_full, symbol='AUSDT')
    pos_b = _seed_one(pm_full, symbol='BUSDT', entry=2.0, qty=5.0, original_qty=5.0)
    positions = {'AUSDT': pos_a, 'BUSDT': pos_b}
    ok = pm_full['pm']._close('AUSDT', pos_a, 1.1, '时间止损', positions)
    assert ok is True
    assert 'AUSDT' not in positions
    assert positions['BUSDT'] is pos_b
    stored = pm_full['redis'].get('pm:positions')
    assert 'BUSDT' in stored and 'AUSDT' not in stored
