"""Golden：PM 的 PnL 计算（以当前实现推导 expected，不自行设计"正确 PnL"）。

公式冻结（源自 _close 实盘路径当前实现）：
  LONG : pnl_u  = round((price - entry) * qty, 2)   pnl_pct = (price-entry)/entry*100
  SHORT: pnl_u  = round((entry - price) * qty, 2)   pnl_pct = (entry-price)/entry*100

注意（Observed Current Behavior）：沙盘平仓路径不日志化 pnl；pnl 只在实盘
路径以 `pnl=+10.0% (+100.00U)` 形式日志化——因此本文件走实盘路径冻结。
"""
from pm_golden_helpers import (
    make_position,
    recorded_entry,
    recorded_price,
    recorded_qty,
)


def _real_close(pm_full, *, side, entry, price, qty, symbol='AUSDT'):
    """实盘平仓路径：positionRisk 有仓 → 市价单成交 → 确认已平。"""
    pm_full['set_sandbox'](False)
    pos = make_position(entry=entry, qty=qty, original_qty=qty, side=side)
    positions = {symbol: pos}
    amt = f'-{qty}' if side == 'SHORT' else f'{qty}'
    reads = {'n': 0}

    def fake_fapi_get(path, params=None):
        reads['n'] += 1
        if reads['n'] == 1:
            return [{'symbol': symbol, 'positionAmt': amt, 'entryPrice': str(entry)}]
        return []                                          # 平仓后确认已平

    pm_full['set_s6api'](
        fapi_get=fake_fapi_get,
        fapi_post=lambda path, params=None: {'orderId': 1, 'status': 'FILLED',
                                             'executedQty': str(qty)})
    ok = pm_full['pm']._close(symbol, pos, price, '手动平仓', positions)
    assert ok is True
    rec = pm_full['calls']['record_trade'][0]
    return rec, pm_full['calls']['logs']


def test_long_win_pnl(pm_full):
    """LONG entry=100 exit=110 qty=10 → pnl=+10.0% (+100.00U)。"""
    rec, logs = _real_close(pm_full, side='LONG', entry=100.0, price=110.0, qty=10.0)
    assert recorded_entry(rec) == 100.0 and recorded_price(rec) == 110.0
    assert recorded_qty(rec) == 10.0
    assert any('pnl=+10.0% (+100.00U)' in m for m in logs)


def test_long_loss_pnl(pm_full):
    """LONG entry=100 exit=90 qty=10 → pnl=-10.0% (-100.00U)。"""
    rec, logs = _real_close(pm_full, side='LONG', entry=100.0, price=90.0, qty=10.0)
    assert recorded_entry(rec) == 100.0 and recorded_price(rec) == 90.0
    assert any('pnl=-10.0% (-100.00U)' in m for m in logs)


def test_short_win_pnl(pm_full):
    """SHORT entry=100 exit=90 qty=10 → pnl=+10.0% (+100.00U)。"""
    rec, logs = _real_close(pm_full, side='SHORT', entry=100.0, price=90.0, qty=10.0)
    assert recorded_entry(rec) == 100.0 and recorded_price(rec) == 90.0
    assert any('pnl=+10.0% (+100.00U)' in m for m in logs)


def test_short_loss_pnl(pm_full):
    """SHORT entry=100 exit=110 qty=10 → pnl=-10.0% (-100.00U)。"""
    rec, logs = _real_close(pm_full, side='SHORT', entry=100.0, price=110.0, qty=10.0)
    assert recorded_entry(rec) == 100.0 and recorded_price(rec) == 110.0
    assert any('pnl=-10.0% (-100.00U)' in m for m in logs)


def test_zero_pnl(pm_full):
    """exit == entry → pnl=+0.0% (+0.00U)。"""
    _, logs = _real_close(pm_full, side='LONG', entry=100.0, price=100.0, qty=10.0)
    assert any('pnl=+0.0% (+0.00U)' in m for m in logs)


def test_fractional_qty_and_price_rounding(pm_full):
    """小数数量/价格：pnl_u 按 round(x, 2) 冻结。"""
    # LONG entry=1.0 exit=1.05 qty=0.333 → 0.333*0.05 = 0.01665 → round → +0.02U
    rec, logs = _real_close(pm_full, side='LONG', entry=1.0, price=1.05, qty=0.333)
    assert recorded_qty(rec) == 0.333
    assert any('pnl=+5.0% (+0.02U)' in m for m in logs)

    # dust：exit=1.0001 → pnl_u = round(0.0000333, 2) = 0.0
    # （换 symbol：同 symbol 的第二次 close 会被 4h 防重入标记拦截——该行为已冻结）
    _, logs2 = _real_close(pm_full, side='LONG', entry=1.0, price=1.0001, qty=0.333,
                           symbol='BUSDT')
    assert any('pnl=+0.0% (+0.00U)' in m for m in logs2)


def test_partial_close_long_pnl_direction_and_remaining(pm_full):
    """分层止盈 LONG：pnl_u = (price-entry)*close_qty，剩余数量按 round(x,4) 冻结。"""
    pm_full['set_sandbox'](True)
    pos = make_position(entry=100.0, qty=10.0, original_qty=10.0, side='LONG')
    positions = {'AUSDT': pos}
    pm_full['pm']._partial_close('AUSDT', pos, 105.0, 10.0 * 0.5, 5, positions)
    assert pos['qty'] == 5.0                                  # round(10 - 5, 4)
    assert any('25.00USDT' in m for m in pm_full['calls']['logs'])
    assert any('剩余=5.0' in m for m in pm_full['calls']['logs'])
    # 分层止盈当前不产生 record_trade（最终平仓时统一记账）——Observed Current Behavior
    assert pm_full['calls']['record_trade'] == []


def test_partial_close_short_pnl_direction(pm_full):
    """分层止盈 SHORT：pnl_u = (entry-price)*close_qty。"""
    pm_full['set_sandbox'](True)
    pos = make_position(entry=100.0, qty=10.0, original_qty=10.0, side='SHORT')
    positions = {'AUSDT': pos}
    pm_full['pm']._partial_close('AUSDT', pos, 90.0, 4.0, 5, positions)
    assert pos['qty'] == 6.0
    assert any('40.00USDT' in m for m in pm_full['calls']['logs'])
    assert any('剩余=6.0' in m for m in pm_full['calls']['logs'])


def test_partial_close_multiple_times(pm_full):
    """连续两次部分平仓：qty 依次递减，每次按当次 close_qty 记 pnl。"""
    pm_full['set_sandbox'](True)
    pos = make_position(entry=100.0, qty=10.0, original_qty=10.0, side='LONG')
    positions = {'AUSDT': pos}
    pm_full['pm']._partial_close('AUSDT', pos, 105.0, 2.0, 5, positions)
    pm_full['pm']._partial_close('AUSDT', pos, 108.0, 3.0, 5, positions)
    assert pos['qty'] == 5.0
    assert any('10.00USDT' in m for m in pm_full['calls']['logs'])   # 2*5
    assert any('24.00USDT' in m for m in pm_full['calls']['logs'])   # 3*8
