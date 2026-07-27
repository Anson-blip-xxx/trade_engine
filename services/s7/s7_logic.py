#!/usr/bin/env python3
"""
s7_logic.py - 策略逻辑层（热reload目标）
所有网格策略逻辑、市场评分、订单管理、库存退出等
"""
import json, time, subprocess
from datetime import datetime

import s7_core as core

# === 状态防抖（模块级，reload 时保留）===
_state_prev = {}
_state_cur  = {}
_last_s7_snap = 0

# ============================================================
# 市场评分 & 状态检测
# ============================================================

def score_market(symbol):
    """
    5维度评分，返回 (score, price, ema20, ema60, atr)
    score > 0 偏多，< 0 偏空，范围 -7 ~ +7
    """
    price = core.get_price(symbol)
    ema20 = core.get_ema(symbol, 20, '1h')
    ema60 = core.get_ema(symbol, 60, '1h')
    atr   = core.get_atr(symbol)
    score = 0

    score += 2 if ema20 > ema60 else -2
    score += 1 if price > ema20 else -1

    klines = core.get_klines(symbol, '1h', 22)
    if len(klines) >= 22:
        atrs = [max(float(k[2])-float(k[3]),
                    abs(float(k[2])-float(klines[i-1][4])),
                    abs(float(k[3])-float(klines[i-1][4])))
                for i, k in enumerate(klines[1:], 1)]
        avg_atr = sum(atrs[:-2]) / len(atrs[:-2]) if atrs[:-2] else atr
        score += 1 if atr < avg_atr else -1

    if len(klines) >= 6:
        recent = klines[-5:]
        bearish = sum(1 for k in recent if float(k[4]) < float(k[1]))
        bullish = 5 - bearish
        if bullish >= 4:   score += 2
        elif bullish >= 3: score += 1
        elif bearish >= 4: score -= 2
        elif bearish >= 3: score -= 1

    if len(klines) >= 9:
        vols = [float(k[5]) for k in klines]
        recent_vol = sum(vols[-3:]) / 3
        base_vol   = sum(vols[-8:-3]) / 5
        if base_vol > 0:
            score += 1 if recent_vol < base_vol else -1

    return score, price, ema20, ema60, atr


def detect_market_state(symbol):
    """
    统一从 s0 读取市场状态（不再本地计算）
    返回: (regime, regime_score)
    """
    try:
        import sys as _sys
        _sys.path.insert(0, '/root/.openclaw/trade/s0-market-guard')
        from s0_reader import load_market_state as _load_s0
        _s0 = _load_s0()
        if _s0 and 'regime' in _s0:
            return (_s0['regime'], _s0['regime_score'])
        return ('range', 0)
    except Exception:
        pass
    return ('range', 0)
def calc_recovery_score(symbol: str, state: dict) -> float:
    """6维恢复评分：Shock降温/ATR收缩/EMA收敛/成交量回归/时间衰减/价格恢复"""
    rec = state.get('recovery', {}).get(symbol, {})
    snap = rec.get('snapshot', {})
    if not snap:
        return 0.0

    score = 0.0

    # 1. Shock 降温
    shock, _heat = core.get_s2_shock_score(symbol)
    if shock < 3:
        score += 3
    elif shock < 5:
        score += 1

    # 2. ATR 收缩
    atr = core.get_atr(symbol)
    if atr and snap.get('atr', 0) > 0:
        ratio = atr / snap['atr']
        if ratio < 0.5:
            score += 2
        elif ratio < 0.7:
            score += 1

    # 3. EMA 收敛
    ema20 = core.get_ema(symbol, 20)
    ema60 = core.get_ema(symbol, 60)
    if ema20 and ema60:
        cur_gap = abs(ema20 - ema60)
        snap_gap = snap.get('ema_gap', cur_gap)
        if snap_gap > 0 and cur_gap < snap_gap * 0.7:
            score += 2

    # 4. 成交量回归
    klines = core.get_klines(symbol, '1h', 3)
    if klines and snap.get('volume', 0) > 0:
        cur_vol = sum(float(k[5]) for k in klines[-2:]) / 2
        ratio = cur_vol / snap['volume']
        if ratio < 0.5:
            score += 2
        elif ratio < 0.7:
            score += 1

    # 5. 时间衰减
    risk_off_start = rec.get('risk_off_start', 0)
    if risk_off_start and (time.time() - risk_off_start) > 86400:
        score += 1

    # 6. 价格恢复
    price = core.get_price(symbol)
    snap_price = snap.get('price', 0)
    if price and snap_price > 0:
        recovery_pct = (price - snap_price) / snap_price
        if recovery_pct > 0.05:
            score += 1

    return min(score, 10.0)


def _take_risk_off_snapshot(symbol: str, state: dict) -> dict:
    """进入 risk-off 时立即记录基准数据（不重复）"""
    rec = state.setdefault('recovery', {}).setdefault(symbol, {})
    if rec.get('state') == 'risk-off' and rec.get('snapshot'):
        return state  # 已有快照

    price = core.get_price(symbol)
    atr = core.get_atr(symbol)
    ema20 = core.get_ema(symbol, 20)
    ema60 = core.get_ema(symbol, 60)
    klines = core.get_klines(symbol, '1h', 3)
    cur_vol = sum(float(k[5]) for k in klines[-2:]) / 2 if klines else 0
    shock, _heat = core.get_s2_shock_score(symbol)

    rec.update({
        'state': 'risk-off',
        'state_enter_time': time.time(),
        'risk_off_start': rec.get('risk_off_start', time.time()),  # 首次记录
        'snapshot': {
            'price': price,
            'atr': atr or 0,
            'volume': cur_vol,
            'shock': shock,
            'ema_gap': abs(ema20 - ema60) if ema20 and ema60 else 0,
            'timestamp': time.time(),
        },
    })
    return state


def _run_recovery_engine(symbol: str, market_state: str, state: dict) -> tuple:
    """
    在 market_state 基础上叠加恢复逻辑，返回 (effective_state, state)
    状态闭环比：RISK_OFF → WATCH → RANGE
    """
    rec = state.setdefault('recovery', {}).setdefault(symbol, {})
    now = time.time()
    MIN_STAY = 1800  # 30分钟防抖

    # 进入 risk-off：记录快照
    if market_state == 'risk-off':
        if rec.get('state') != 'risk-off':
            state = _take_risk_off_snapshot(symbol, state)
            core.log(f'[Recovery] {symbol} → RISK_OFF 快照已记录')
        return 'risk-off', state

    # 已经在 WATCH 或 risk-off 里，检查是否可以升级
    rec_state = rec.get('state', '')

    if rec_state == 'risk-off':
        if now - rec.get('state_enter_time', now) < MIN_STAY:
            return 'risk-off', state
        score = calc_recovery_score(symbol, state)
        rec['score'] = round(score, 2)
        if score >= 4:
            rec['state'] = 'watch'
            rec['state_enter_time'] = now
            rec['watch_start'] = now
            core.log(f'[Recovery] {symbol} RISK_OFF → WATCH (score={score:.1f})')
            core.tg(f'👀 *{symbol}* Risk-Off → Watch\nRecovery Score: {score:.1f}\n进入恢复观察阶段')
            return 'watch', state
        return 'risk-off', state

    if rec_state == 'watch':
        if now - rec.get('state_enter_time', now) < MIN_STAY:
            return 'watch', state
        score = calc_recovery_score(symbol, state)
        rec['score'] = round(score, 2)
        watch_duration = now - rec.get('watch_start', now)
        if score >= 7 and watch_duration >= 900:  # score>=7 且 WATCH >=15min
            rec['state'] = 'range'
            rec['state_enter_time'] = now
            rec['snapshot'] = {}  # 清空快照，等下次 risk-off 重新记录
            core.log(f'[Recovery] {symbol} WATCH → RANGE (score={score:.1f})')
            core.tg(f'✅ *{symbol}* Watch → Range\nRecovery Score: {score:.1f}\n网格恢复运行')
            return market_state, state  # 回到 MarketGuard 评分决定的正常状态
        return 'watch', state

    # 没有 recovery 状态或已是 range，跟随 market_state
    if rec_state == 'range' or not rec_state:
        # 如果 market_state 重新变差，清除 recovery 状态
        if market_state in ('risk-off', 'weak_bear'):
            rec['state'] = ''
        return market_state, state

    return market_state, state


def should_escape(symbol):
    """趋势逃生检测"""
    try:
        price = core.get_price(symbol)
        ema20 = core.get_ema(symbol, 20, '1h')
        ema60 = core.get_ema(symbol, 60, '1h')
        atr   = core.get_atr(symbol)

        ema_distance = abs(ema20 - ema60) / price if price > 0 else 0

        klines = core.get_klines(symbol, '1h', 30)
        if len(klines) >= 30:
            atrs = []
            for i in range(1, len(klines)):
                h, l, pc = float(klines[i][2]), float(klines[i][3]), float(klines[i-1][4])
                atrs.append(max(h-l, abs(h-pc), abs(l-pc)))
            avg_atr = sum(atrs[:-1]) / len(atrs[:-1]) if atrs else atr
            atr_spike = atr > avg_atr * 1.5
        else:
            atr_spike = False

        if ema_distance > 0.03:
            core.log(f'[逃生] {symbol} EMA发散{ema_distance:.1%}>3%')
            return True
        if atr_spike:
            core.log(f'[逃生] {symbol} ATR突然放大{atr/avg_atr:.1f}x')
            return True
        return False
    except:
        return False


# ============================================================
# 库存退出引擎
# ============================================================

def inventory_exit(symbol, inventory, exit_mode):
    if inventory <= 0:
        return

    if exit_mode == 'passive':
        orders = core.get_open_orders(symbol)
        for o in orders:
            if o['side'] == 'BUY':
                core.fapi_delete('/fapi/v1/order', {'symbol': symbol, 'orderId': o['orderId']})
        core.log(f'[被动退出] {symbol} 撤销买单，保留卖单等反弹')
        core.tg(f'🟡 *{symbol}* 被动退出：撤买单，等反弹减仓')

    elif exit_mode == 'aggressive':
        reduce_qty = inventory * 0.2
        r = core.market_close(symbol, reduce_qty)
        if 'orderId' in r:
            core.log(f'[主动退出] {symbol} 减仓20% qty={reduce_qty:.4f}')
            core.tg(f'🟠 *{symbol}* 主动退出：减仓20%')

    elif exit_mode == 'emergency':
        core.cancel_all_orders(symbol)
        r = core.market_close(symbol, inventory)
        if 'orderId' in r:
            core.log(f'[紧急平仓] {symbol} 全部平仓 qty={inventory:.4f}')
            core.tg(f'🔴 *{symbol}* 紧急平仓：全部市价清算')


# ============================================================
# 网格构建 & 订单管理
# ============================================================

def build_grid(center_price, atr, qty_prec, price_prec, capital):
    step = round(atr * core.ATR_MULTIPLIER, price_prec)
    if step <= 0:
        return []

    order_size = capital / (core.GRID_LAYERS * 2)
    levels = []
    for i in range(1, core.GRID_LAYERS + 1):
        buy_price  = round(center_price - i * step, price_prec)
        sell_price = round(center_price + i * step, price_prec)
        buy_qty    = round(order_size / buy_price, qty_prec)
        sell_qty   = round(order_size / sell_price, qty_prec)

        if buy_qty > 0:
            levels.append({'side': 'BUY',  'price': buy_price,  'qty': buy_qty})
        if sell_qty > 0:
            levels.append({'side': 'SELL', 'price': sell_price, 'qty': sell_qty})

    return levels


def adjust_order_size(base_size, inventory, max_inventory):
    if max_inventory <= 0:
        return base_size
    ratio = abs(inventory) / max_inventory
    if ratio > 0.8:
        return base_size * 0.3
    if ratio > 0.5:
        return base_size * 0.6
    return base_size


def _ch_write_fee_log(symbol, decision, reason, spread_pct, min_required_pct, market_state=''):
    try:
        row = json.dumps({
            'log_time': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'symbol': symbol, 'decision': decision, 'reason': reason,
            'spread_pct': round(spread_pct, 6),
            'min_required_pct': round(min_required_pct, 6),
            'market_state': market_state,
        })
        subprocess.run(
            ['clickhouse-client', '-q', 'INSERT INTO default.s7_fee_engine_logs FORMAT JSONEachRow'],
            input=row, text=True, timeout=3, capture_output=True
        )
    except Exception as e:
        core.log(f'[CH] fee_engine_log write error: {e}')


def is_grid_profitable(symbol, center, step, fee_rate=0.0004):
    if center <= 0 or step <= 0:
        return False
    grid_step_pct = step / center
    min_required  = fee_rate * 2 * 3
    if grid_step_pct < min_required:
        core.log(f'[Fee Engine] 网格间距{grid_step_pct:.4%}<最低要求{min_required:.4%}，不值得做市')
        _ch_write_fee_log(symbol, 'reject', f'网格间距{grid_step_pct:.4%}<最低要求{min_required:.4%}',
                          grid_step_pct, min_required)
        return False
    _ch_write_fee_log(symbol, 'approve', f'网格间距{grid_step_pct:.4%}>=最低要求{min_required:.4%}',
                      grid_step_pct, min_required)
    return True


MIN_NOTIONAL = 50.0


def place_limit_order(symbol, side, price, qty, price_prec):
    if price * qty < MIN_NOTIONAL:
        core.log(f'[跳过] {symbol} {side} 名义价值{price*qty:.2f}U < {MIN_NOTIONAL}U')
        return {}
    r = core.fapi_post('/fapi/v1/order', {
        'symbol':      symbol,
        'side':        side,
        'type':        'LIMIT',
        'price':       f'{price:.{price_prec}f}',
        'quantity':    qty,
        'timeInForce': 'GTC',
    })
    return r


def reconcile_orders(symbol, state) -> bool:
    grid_info = state['grids'].get(symbol, {})
    if not grid_info.get('active'):
        return False
    expected = grid_info.get('placed', 0)
    if expected == 0:
        return False
    try:
        actual_orders = core.get_open_orders(symbol)
        actual_count = len(actual_orders)
        if actual_count >= expected * 0.6:
            core.log(f'[{symbol}] 对账OK: 实际{actual_count}单 / 期望{expected}单，跳过重建')
            return True
        else:
            core.log(f'[{symbol}] 对账失败: 实际{actual_count}单 / 期望{expected}单，需重建')
            return False
    except Exception as e:
        core.log(f'[{symbol}] 对账异常: {e}，保守重建')
        return False


# ============================================================
# SSS-1: Inventory Cost Engine
# ============================================================

def update_inventory_cost(grid_info, fill_price, fill_qty, side):
    """只更新 avg_cost / inventory_qty。realized_pnl 由调用方（_sync_fills）统一处理。"""
    avg_cost  = grid_info.get('avg_cost', fill_price)
    inventory = grid_info.get('inventory_qty', 0.0)

    if side == 'BUY':
        total_cost = avg_cost * inventory + fill_price * fill_qty
        inventory  = inventory + fill_qty
        avg_cost   = total_cost / inventory if inventory > 0 else fill_price
    else:
        inventory  = max(0.0, inventory - fill_qty)
        if inventory == 0:
            avg_cost = 0.0

    grid_info['avg_cost']     = avg_cost
    grid_info['inventory_qty'] = inventory
    return grid_info


def _ch_write_fill(symbol, side, price, qty, fee_usdt, avg_cost, inventory, realized_pnl, fill_id, market_state=''):
    try:
        row = json.dumps({
            'fill_time': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'symbol': symbol, 'side': side,
            'price': price, 'qty': qty, 'fee_usdt': fee_usdt,
            'avg_cost': avg_cost, 'inventory': inventory,
            'realized_pnl': realized_pnl, 'fill_id': fill_id,
            'market_state': market_state,
        })
        subprocess.run(
            ['clickhouse-client', '-q',
             'INSERT INTO default.s7_grid_fills FORMAT JSONEachRow'],
            input=row, text=True, timeout=3, capture_output=True
        )
    except Exception as e:
        core.log(f'[CH] s7_grid_fills write error: {e}')


def _sync_fills(symbol, state):
    """同步成交记录，更新网格状态"""
    grid_info = state['grids'].get(symbol, {})
    last_fill_id = grid_info.get('last_fill_id', 0)

    try:
        trades = core.fapi_get('/fapi/v1/userTrades', {'symbol': symbol, 'limit': 50})
        if not isinstance(trades, list):
            return state

        new_fills = [t for t in trades if int(t['id']) > last_fill_id]
        for t in sorted(new_fills, key=lambda x: int(x['id'])):
            side     = 'BUY' if t['buyer'] else 'SELL'
            price    = float(t['price'])
            qty      = float(t['qty'])
            fee_usdt = float(t['commission']) if t['commissionAsset'] == 'USDT' else float(t['commission']) * float(t['price'])
            if side == 'SELL':
                avg_cost_before = grid_info.get('avg_cost', price)
                cycle_profit_this = (price - avg_cost_before) * qty - fee_usdt if avg_cost_before > 0 else -fee_usdt
                grid_info['cycle_count'] = grid_info.get('cycle_count', 0) + 1
                grid_info['cycle_profit'] = grid_info.get('cycle_profit', 0.0) + cycle_profit_this
            grid_info = update_inventory_cost(grid_info, price, qty, side)
            if side == 'SELL':
                grid_info['realized_pnl'] = grid_info.get('realized_pnl', 0) + cycle_profit_this
            else:
                grid_info['realized_pnl'] = grid_info.get('realized_pnl', 0) - fee_usdt
            grid_info['last_fill_id'] = int(t['id'])
            _ch_write_fill(
                symbol=symbol, side=side, price=price, qty=qty, fee_usdt=fee_usdt,
                avg_cost=grid_info.get('avg_cost', 0),
                inventory=grid_info.get('inventory_qty', 0),
                realized_pnl=grid_info.get('realized_pnl', 0),
                fill_id=int(t['id']),
            )

        state['grids'][symbol] = grid_info
    except Exception as e:
        core.log(f'[_sync_fills] {symbol} 异常: {e}')
    return state


# ============================================================
# manage_grid 子函数（架构拆分 P1）
# ============================================================

def _check_shock(symbol, state) -> str:
    """
    S2 Shock Filter — 全局+symbol级
    返回 'global_pause' / 'risk_off' / 'watch' / 'ok'
    副作用：可能修改 state（紧急退出、全局暂停等）
    """
    if state.get('s2_global_pause'):
        return 'global_pause'

    shock, heat = core.get_s2_shock_score(symbol)

    if heat >= 10:
        state['s2_global_pause'] = True
        core.log(f'[{symbol}] S2市场过热(heat={heat})，全局暂停')
        core.tg(f'🌡 *S2 Shock* 市场过热(heat={heat})，全局暂停')
        return 'global_pause'

    if shock >= 8:
        core.log(f'[{symbol}] S2 Shock level=HIGH(shock={shock})，紧急退出')
        core.cancel_all_orders(symbol)
        inventory = core.get_inventory(symbol)
        inventory_exit(symbol, inventory, 'emergency')
        grid_info = state['grids'].get(symbol, {})
        kept = {k: grid_info[k] for k in ('avg_cost','inventory_qty','realized_pnl','last_fill_id','cycle_count','cycle_profit') if k in grid_info}
        kept['active'] = False
        state['grids'][symbol] = kept
        core.tg(f'🔴 *{symbol}* S2 Shock(shock={shock}) → 紧急退出')
        return 'risk_off'

    if shock >= 6:
        core.log(f'[{symbol}] S2 Shock level=WATCH(shock={shock})，停止新增')
        orders = core.get_open_orders(symbol)
        for o in orders:
            if o['side'] == 'BUY':
                core.fapi_delete('/fapi/v1/order', {'symbol': symbol, 'orderId': o['orderId']})
        grid_info = state['grids'].get(symbol, {})
        state['grids'][symbol] = {**grid_info, 'active': False, 'paused_reason': 's2_watch'}
        return 'watch'

    return 'ok'


def _check_market_state(symbol) -> tuple:
    """
    市场状态检测+逃生判断
    返回 (market_state, score, price, inventory, max_inventory, escape)
    """
    market_state, score = detect_market_state(symbol)
    inventory = core.get_inventory(symbol)
    price = core.get_price(symbol)
    capital_per_sym = core.TOTAL_CAPITAL / len(core.GRID_SYMBOLS)
    max_inventory = capital_per_sym * core.MAX_INV_RATIO / price if price > 0 else 0
    escape = should_escape(symbol)
    core.log(f'[{symbol}] 市场状态: {market_state}(score={score:+d}) 库存: {inventory:.4f}')
    return market_state, score, price, inventory, max_inventory, escape


def _check_inventory_risk(symbol, grid_info, price, inventory, max_inventory):
    """
    库存风险+回撤检查，返回 (stop_buy, reduce_buy) 两个布尔标志
    副作用：超阈值/深度回撤时执行退出
    注意：调用方应在 _check_inventory_risk 返回后检查 state['grids'][symbol]['active']
    """
    stop_buy = False
    reduce_buy = False

    # 库存超阈值 → 被动退出
    if inventory > max_inventory:
        core.log(f'[{symbol}] 库存{inventory:.4f}超阈值{max_inventory:.4f}，被动退出')
        inventory_exit(symbol, inventory, 'passive')
        kept = {k: grid_info[k] for k in ('avg_cost','inventory_qty','realized_pnl','last_fill_id','cycle_count','cycle_profit') if k in grid_info}
        kept['active'] = False
        return stop_buy, reduce_buy, False, kept

    # 空仓浮亏止损（短头寸price上涨即亏损）
    avg_cost = grid_info.get('avg_cost', 0)
    if avg_cost > 0 and inventory < 0:
        dd = (avg_cost - price) / avg_cost  # 空仓：价格上涨=亏损
        if dd < -0.15:
            core.log(f'[{symbol}] 空仓浮亏{dd:.1%} < -15%，强制平仓')
            core.market_close(symbol, inventory)
            kept = {k: grid_info[k] for k in ('avg_cost','inventory_qty','realized_pnl','last_fill_id','cycle_count','cycle_profit') if k in grid_info}
            kept['active'] = False
            return stop_buy, reduce_buy, False, kept
        elif dd < -0.08:
            core.log(f'[{symbol}] 空仓浮亏{dd:.1%} < -8%，停止新增空单')
            stop_buy = True

    # 回撤检测（多仓）
    if avg_cost > 0 and inventory > 0:
        dd = (price - avg_cost) / avg_cost
        if dd < -0.25:
            core.log(f'[{symbol}] Drawdown {dd:.1%} < -25%，强制清仓')
            inventory_exit(symbol, inventory, 'emergency')
            kept = {k: grid_info[k] for k in ('avg_cost','inventory_qty','realized_pnl','last_fill_id','cycle_count','cycle_profit') if k in grid_info}
            kept['active'] = False
            return stop_buy, reduce_buy, False, kept
        elif dd < -0.20:
            core.log(f'[{symbol}] Drawdown {dd:.1%} < -20%，主动减仓')
            inventory_exit(symbol, inventory, 'aggressive')
            kept = {k: grid_info[k] for k in ('avg_cost','inventory_qty','realized_pnl','last_fill_id','cycle_count','cycle_profit') if k in grid_info}
            kept['active'] = False
            return stop_buy, reduce_buy, False, kept
        elif dd < -0.15:
            core.log(f'[{symbol}] Drawdown {dd:.1%} < -15%，停止补仓')
            stop_buy = True
        elif dd < -0.10:
            core.log(f'[{symbol}] Drawdown {dd:.1%} < -10%，减少50%买单')
            reduce_buy = True

    return stop_buy, reduce_buy, True, None


def _check_expectancy(symbol, grid_info, state) -> bool:
    """期望值检查，返回 True=暂停"""
    if grid_info.get('cycle_count', 0) >= 30:
        expectancy = grid_info['cycle_profit'] / grid_info['cycle_count']
        core.log(f'[{symbol}] Expectancy检查: count={grid_info["cycle_count"]} profit={grid_info["cycle_profit"]:.4f} avg={expectancy:.4f}')
        if expectancy < 0:
            core.log(f'[{symbol}] Expectancy负值({expectancy:.4f})，暂停网格')
            core.cancel_all_orders(symbol)
            state['grids'][symbol] = {**grid_info, 'active': False, 'paused_reason': 'negative_expectancy'}
            core.tg(f'⚠ *{symbol}* Expectancy负({expectancy:.4f})，暂停网格')
            return True
    return False


def _build_or_update_grid(symbol, state, grid_info, market_state, price, inventory, max_inventory,
                           capital_per_sym, stop_buy, reduce_buy):
    """执行网格建立或增量更新"""
    atr         = core.get_atr(symbol)
    center      = core.get_ema(symbol, 20, '1h')
    qty_prec, price_prec = core.get_symbol_info(symbol)
    last_center = grid_info.get('center', 0)

    REGIME_FACTOR = {'bull_trend': 0.3, 'weak_bull': 0.5, 'range': 0.8, 'weak_bear': 1.0, 'risk-off': 1.2}
    step = round(atr * core.ATR_MULTIPLIER * REGIME_FACTOR.get(market_state, 0.8), price_prec)
    core.log(f'[{symbol}] Regime ATR: state={market_state} factor={REGIME_FACTOR.get(market_state, 0.8):.1f} step={step:.{price_prec}f}')

    # SSS-2: Fee Engine
    if not is_grid_profitable(symbol, center, step):
        core.log(f'[{symbol}] 网格利润不足，跳过')
        return state

    if not grid_info.get('active'):
        need_full_rebuild = True
        shift_layers = 0
    else:
        shift_layers = round((center - last_center) / step) if step > 0 else 0
        need_full_rebuild = abs(shift_layers) >= core.GRID_LAYERS

    if need_full_rebuild:
        core.log(f'[{symbol}] 全量建立网格 center={center:.4f} atr={atr:.4f}')
        core.cancel_all_orders(symbol)
        levels = build_grid(center, atr, qty_prec, price_prec, capital_per_sym)

        imb = core.get_imbalance(symbol)
        if imb > 0.65:
            buy_bias, sell_bias = 1.4, 0.6
        elif imb < 0.35:
            buy_bias, sell_bias = 0.6, 1.4
        else:
            buy_bias, sell_bias = 1.0, 1.0
        if imb != 0.5:
            core.log(f'[{symbol}] Orderbook imbalance={imb:.2f} buy_bias={buy_bias} sell_bias={sell_bias}')

        placed = 0
        for lvl in levels:
            if lvl['side'] == 'BUY':
                if stop_buy:
                    continue
                qty = round(lvl['qty'] * buy_bias, qty_prec)
                if reduce_buy:
                    qty = round(qty * 0.5, qty_prec)
                qty = round(adjust_order_size(qty, inventory, max_inventory), qty_prec)
            else:
                qty = round(lvl['qty'] * sell_bias, qty_prec)
            if qty <= 0:
                continue
            r = place_limit_order(symbol, lvl['side'], lvl['price'], qty, price_prec)
            if 'orderId' in r:
                placed += 1
            time.sleep(0.1)

        preserved = {k: grid_info.get(k) for k in ('avg_cost','realized_pnl','inventory_qty','last_fill_id','cycle_count','cycle_profit') if k in grid_info}
        state['grids'][symbol] = {'active': True, 'center': center,
                                   'last_update': int(time.time()), 'placed': placed, **preserved}
        core.log(f'[{symbol}] 网格建立完成，挂单{placed}个')
        core.tg(f'🔲 *{symbol}* 网格已建立\n中心价: {center:.4f}\nATR: {atr:.4f}\n挂单: {placed}个')

    elif shift_layers != 0:
        core.log(f'[{symbol}] 增量更新网格 偏移{shift_layers:+d}层')
        orders = core.get_open_orders(symbol)
        order_prices = {str(round(float(o['price']), price_prec)): o['orderId'] for o in orders}

        for i in range(abs(shift_layers)):
            if shift_layers > 0:
                target = round(last_center - (core.GRID_LAYERS - i) * step, price_prec)
            else:
                target = round(last_center + (core.GRID_LAYERS - i) * step, price_prec)
            oid = order_prices.get(str(round(target, price_prec)))
            if oid:
                core.fapi_delete('/fapi/v1/order', {'symbol': symbol, 'orderId': oid})
                time.sleep(0.05)

        order_size = capital_per_sym / (core.GRID_LAYERS * 2)
        for i in range(abs(shift_layers)):
            if shift_layers > 0:
                new_price = round(center + (core.GRID_LAYERS - i) * step, price_prec)
                side = 'SELL'
            else:
                new_price = round(center - (core.GRID_LAYERS - i) * step, price_prec)
                side = 'BUY'
            if side == 'BUY' and stop_buy:
                continue
            qty = round(order_size / new_price, qty_prec)
            if side == 'BUY':
                if reduce_buy:
                    qty = round(qty * 0.5, qty_prec)
                qty = round(adjust_order_size(qty, inventory, max_inventory), qty_prec)
            if qty > 0:
                place_limit_order(symbol, side, new_price, qty, price_prec)
                time.sleep(0.05)

        state['grids'][symbol] = {**grid_info, 'center': center, 'last_update': int(time.time())}

    return state


# ============================================================
# 单币种网格管理（编排层）
# ============================================================

def manage_grid(symbol, state):
    """网格策略主入口 — 编排各子函数"""

    # ---- 重启对账（首次运行） ----
    if not state.get('_reconciled', {}).get(symbol):
        state.setdefault('_reconciled', {})[symbol] = True
        if reconcile_orders(symbol, state):
            state = _sync_fills(symbol, state)
            grid_info = state['grids'].get(symbol, {})
            price = core.get_price(symbol)
            avg_cost = grid_info.get('avg_cost', price)
            inventory = core.get_inventory(symbol)
            realized_pnl = grid_info.get('realized_pnl', 0.0)
            unrealized_pnl = (price - avg_cost) * inventory if avg_cost > 0 else 0.0
            core.log(f'[{symbol}] 已实现PnL: {realized_pnl:.2f}U 未实现: {unrealized_pnl:.2f}U 库存: {inventory:.4f}')
            return state

    # ---- S2 Shock Filter ----
    shock = _check_shock(symbol, state)
    if shock in ('global_pause', 'risk_off', 'watch'):
        return state

    # ---- 市场状态 & 逃生 ----
    market_state, score, price, inventory, max_inventory, escape = _check_market_state(symbol)
    capital_per_sym = core.TOTAL_CAPITAL / len(core.GRID_SYMBOLS)

    # ---- Recovery Engine (叠加恢复逻辑，可能覆盖 market_state) ----
    market_state, state = _run_recovery_engine(symbol, market_state, state)

    # ---- 自动重新激活：暂停的网格在条件转好时恢复运行 ----
    grid_info = state['grids'].get(symbol, {})
    if not grid_info.get('active') and market_state not in ('risk-off', 'watch', 'weak_bear', 'bull_trend'):
        # 检查是否有理由保持暂停
        paused_reason = grid_info.get('paused_reason', '')
        should_reactivate = True
        if paused_reason == 'negative_expectancy':
            # expectancy 必须为正才能恢复
            if grid_info.get('cycle_count', 0) >= 30:
                expectancy = grid_info['cycle_profit'] / grid_info['cycle_count']
                should_reactivate = expectancy >= 0.0
            else:  # 不足30单时也可以恢复
                should_reactivate = True
        if should_reactivate:
            core.log(f'[{symbol}] 条件转好，重新激活网格 (market_state={market_state}, paused={paused_reason})')
            state['grids'][symbol] = {**grid_info, 'active': True, 'paused_reason': ''}
            grid_info = state['grids'][symbol]
            grid_info['active'] = True
            core.tg(f'🔄 *{symbol}* 条件转好，网格恢复运行\n状态: {market_state}')
        else:
            core.log(f'[{symbol}] 仍不满足恢复条件 (market_state={market_state}, paused={paused_reason})')
            return state

    # ---- 增强日志：RISK_OFF/WATCH 附加 Recovery Score & 持续时间 ----
    rec = state.get('recovery', {}).get(symbol, {})
    if market_state in ('risk-off', 'watch'):
        rec_state = rec.get('state', '')
        rec_score = rec.get('score', 0.0)
        duration = int((time.time() - rec.get('state_enter_time', time.time())) / 60)
        core.log(f'[{symbol}] 市场状态: {market_state} Recovery: {rec_score:.1f}/10 [{rec_state.upper()} {duration}m]')

    # Risk-Off → 紧急平仓
    if market_state == 'risk-off':
        core.cancel_all_orders(symbol)
        inventory_exit(symbol, inventory, 'emergency')
        grid_info = state['grids'].get(symbol, {})
        kept = {k: grid_info[k] for k in ('avg_cost','inventory_qty','realized_pnl','last_fill_id','cycle_count','cycle_profit') if k in grid_info}
        kept['active'] = False
        state['grids'][symbol] = kept
        return state

    # WATCH → 暂停买入和网格重建（持仓不动，等恢复）等同于 risk-off 但不清仓
    if market_state == 'watch':
        orders = core.get_open_orders(symbol)
        for o in orders:
            if o['side'] == 'BUY':
                core.fapi_delete('/fapi/v1/order', {'symbol': symbol, 'orderId': o['orderId']})
        grid_info = state['grids'].get(symbol, {})
        if grid_info.get('active'):
            state['grids'][symbol] = {**grid_info, 'active': False, 'paused_reason': 'recovery_watch'}
        core.log(f'[{symbol}] WATCH: 暂停买入，等待恢复确认')
        return state

    # 趋势逃生 → 主动减仓
    if escape:
        core.cancel_all_orders(symbol)
        inventory_exit(symbol, inventory, 'aggressive')
        grid_info = state['grids'].get(symbol, {})
        kept = {k: grid_info[k] for k in ('avg_cost','inventory_qty','realized_pnl','last_fill_id','cycle_count','cycle_profit') if k in grid_info}
        kept['active'] = False
        state['grids'][symbol] = kept
        return state

    # weak_bear：只保留卖单，停止买入
    if market_state == 'weak_bear':
        orders = core.get_open_orders(symbol)
        for o in orders:
            if o['side'] == 'BUY':
                core.fapi_delete('/fapi/v1/order', {'symbol': symbol, 'orderId': o['orderId']})
        grid_info = state['grids'].get(symbol, {})
        if grid_info.get('active'):
            core.log(f'[{symbol}] 偏空行情(score={score:+d})，撤买单保留卖单')
        state['grids'][symbol] = {**grid_info, 'active': False}
        return state

    # bull_trend：暂停网格
    if market_state == 'bull_trend':
        grid_info = state['grids'].get(symbol, {})
        if grid_info.get('active'):
            core.log(f'[{symbol}] 强多趋势(score={score:+d})，暂停网格')
        return state

    # S-2: Inventory Lock
    if state.get('inventory_exiting', {}).get(symbol):
        core.log(f'[{symbol}] 库存退出中，跳过重建')
        return state

    # SSS-1: 同步成交记录
    state = _sync_fills(symbol, state)
    grid_info = state['grids'].get(symbol, {})

    # ---- 库存风险 & 回撤 ----
    stop_buy, reduce_buy, grid_ok, kept = _check_inventory_risk(
        symbol, grid_info, price, inventory, max_inventory)
    if not grid_ok:
        state['grids'][symbol] = kept
        return state

    # ---- Expectancy 检查 ----
    if _check_expectancy(symbol, grid_info, state):
        return state

    # ---- 网格建立/更新 ----
    state = _build_or_update_grid(
        symbol, state, grid_info, market_state, price, inventory, max_inventory,
        capital_per_sym, stop_buy, reduce_buy)

    # ---- S-3: 输出PnL ----
    avg_cost = grid_info.get('avg_cost', price)
    realized_pnl = grid_info.get('realized_pnl', 0.0)
    unrealized_pnl = (price - avg_cost) * inventory if avg_cost > 0 else 0.0
    core.log(f'[{symbol}] 已实现PnL: {realized_pnl:.2f}U 未实现: {unrealized_pnl:.2f}U 库存: {inventory:.4f}')

    # ---- equity_snapshot（每5分钟写一次） ----
    global _last_s7_snap
    now_ts = time.time()
    if now_ts - _last_s7_snap >= 300:
        _last_s7_snap = now_ts
        try:
            total_realized = 0.0
            total_unrealized = 0.0
            open_count = 0
            for sym, gi in state.get('grids', {}).items():
                if gi.get('inventory_qty', 0) > 0:
                    open_count += 1
                total_realized += gi.get('realized_pnl', 0.0)
                inv = gi.get('inventory_qty', 0.0)
                if inv > 0:
                    ac = gi.get('avg_cost', 0.0)
                    px = core.get_price(sym) if ac > 0 else 0.0
                    total_unrealized += (px - ac) * inv
            row = json.dumps({
                'snap_time': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
                'system': 's7',
                'total_equity': round(total_realized + total_unrealized, 4),
                'realized_pnl': round(total_realized, 4),
                'unrealized_pnl': round(total_unrealized, 4),
                'open_positions': open_count,
            })
            subprocess.run(
                ['clickhouse-client', '-q', 'INSERT INTO default.equity_snapshot FORMAT JSONEachRow'],
                input=row, text=True, timeout=5, capture_output=True
            )
        except Exception as e:
            core.log(f'[equity_snapshot] s7 write error: {e}')

    return state
