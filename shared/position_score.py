"""持仓实时得分 — S6/S8共用"""
import time

PROTECTION_MIN = 30   # 前30min不参与替换

def calc_position_live_score(symbol: str, pos: dict) -> int:
    """
    计算持仓实时得分，用于替换决策。
    趋势持续走强 → 得分升高（越难被替换）
    趋势出现反转信号 → 得分降低（越容易被替换）
    """
    hold_min = (time.time() - pos.get('open_time', time.time())) / 60
    if hold_min < PROTECTION_MIN:
        return 99  # 保护期内不可被替换

    # 懒加载避免循环引用
    import sys, os
    from shared.market_data import get_price, get_rsi, get_ema

    score     = pos.get('score', 3)
    direction = pos.get('side', 'SHORT')
    price     = get_price(symbol)
    rsi       = get_rsi(symbol)
    ema20     = get_ema(symbol, 20)
    if not pos['entry']:
        return score
    pnl_pct = (pos['entry'] - price) / pos['entry'] * 100 if direction == 'SHORT' \
              else (price - pos['entry']) / pos['entry'] * 100

    if direction == 'SHORT':
        if rsi < 35:              score += 2
        elif rsi < 45:            score += 1
        if price < ema20 * 0.98:  score += 1
        if rsi > 65:              score -= 2
        elif rsi > 55:            score -= 1
        if price > ema20:         score -= 1
    else:  # LONG
        if rsi > 65:              score += 2
        elif rsi > 55:            score += 1
        if price > ema20 * 1.02:  score += 1
        if rsi < 35:              score -= 2
        elif rsi < 45:            score -= 1
        if price < ema20:         score -= 1

    if pnl_pct < -3:                   score -= 1
    if pnl_pct < -6:                   score -= 1
    if hold_min > 360 and pnl_pct < 0: score -= 1

    return max(score, 0)
