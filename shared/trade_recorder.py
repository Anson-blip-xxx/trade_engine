"""
交易记录 — ClickHouse 落库 + Telegram 推送 + PnL 追踪
依赖: binance_api (fapi_get, TG_TOKEN, TG_CHAT_ID), market_data, redis_store, clickhouse_client
"""
import time, json, requests, sys, hashlib
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from shared.binance_api import fapi_get, TG_TOKEN, TG_CHAT_ID
from shared.redis_store import get as _rget, set as _rset
from shared.clickhouse_client import query as _ch_query, query_column, insert as _ch_insert
from shared.postgres_client import upsert_trade_episode as _pg_upsert_trade
from shared.trade_analyzer import enqueue_closed_trade


# ── 周期盈亏 ──
def get_cycle_pnl():
    try:
        checkpoint = _rget('checkpoint:pnl')
        if not checkpoint:
            checkpoint = {'start_ms': int(time.time() * 1000)}
            _rset('checkpoint:pnl', checkpoint)
        start_ms = checkpoint['start_ms']
        total = 0
        for itype in ['REALIZED_PNL', 'FUNDING_FEE', 'COMMISSION']:
            data = fapi_get('/fapi/v1/income', {'incomeType': itype, 'startTime': start_ms, 'limit': 1000})
            if isinstance(data, list):
                total += sum(float(x['income']) for x in data)
        return round(total, 4)
    except Exception as e:
        return None


# ── 亏损冷却 ──
LOSS_COOLDOWN_SEC = 7200

def _write_loss_cooldown(symbol):
    try:
        cd = _rget('cd:loss') or {}
        cd[symbol] = time.time()
        _rset('cd:loss', cd)
    except Exception:
        pass

def _check_loss_cooldown(symbol):
    try:
        cd = _rget('cd:loss')
        if cd and symbol in cd:
            elapsed = time.time() - cd[symbol]
            if elapsed < LOSS_COOLDOWN_SEC:
                return True, int((LOSS_COOLDOWN_SEC - elapsed) / 60)
    except Exception:
        pass
    try:
        _r = _ch_query("SELECT toUnixTimestamp(max(trade_time)) FROM default.trade_history "
                        "WHERE symbol='" + symbol + "' AND result='loss' AND trade_time >= now() - INTERVAL 3 HOUR")
        if _r and _r[0] and _r[0][0] and _r[0][0] != 0:
            ts = _r[0][0]
            elapsed = time.time() - float(ts)
            if elapsed < LOSS_COOLDOWN_SEC:
                remaining = int((LOSS_COOLDOWN_SEC - elapsed) / 60)
                _write_loss_cooldown(symbol)
                return True, remaining
    except Exception:
        pass
    return False, 0


# ── 净值快照 ──
_last_snap = [0]

def _write_equity_snapshot():
    try:
        from shared.s6_auto_trader import load_state, batch_get_prices, get_cycle_pnl, log
        state = load_state()
        positions = state.get('positions', {})
        realized = get_cycle_pnl() or 0.0
        unrealized = 0.0
        if positions:
            real_syms = {k.replace('_SHORT', '') for k in positions}
            price_map = batch_get_prices(list(real_syms))
            for sym, pos in positions.items():
                real_sym = sym.replace('_SHORT', '')
                current_price = price_map.get(real_sym, pos['entry'])
                pnl = (current_price - pos['entry']) * pos['qty']
                if pos.get('side') == 'SHORT':
                    pnl = (pos['entry'] - current_price) * pos['qty']
                unrealized += pnl
        row = json.dumps({
            'snap_time': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S'),
            'system': 's6',
            'total_equity': round(realized + unrealized, 4),
            'realized_pnl': round(realized, 4),
            'unrealized_pnl': round(unrealized, 4),
            'open_positions': len(positions),
        })
        _ch_insert('default.equity_snapshot', row)
    except Exception:
        pass


# ── 记录交易 ──
def _is_duplicate_record(symbol, entry, qty, exit_reason):
    """双进程重复记账防御：2 分钟内已有完全相同的平仓记录（币种/入场/数量/原因）则跳过。

    双进程重复平仓时两进程各记一条，只有 exit_price 有微小差异，
    故以 entry+qty+exit_reason 为键、加 2 分钟时间窗判重。"""
    try:
        _sym = str(symbol).replace("'", "''")
        _reason = str(exit_reason).replace("'", "''")
        _r = _ch_query(
            "SELECT count() FROM default.trade_history "
            f"WHERE symbol='{_sym}' AND entry={float(entry)} AND qty={float(qty)} "
            f"AND exit_reason='{_reason}' "
            "AND trade_time >= now() - INTERVAL 2 MINUTE"
        )
        return bool(_r) and int(_r[0][0]) > 0
    except Exception:
        return False


def _partial_key(position_id):
    digest = hashlib.sha1(str(position_id).encode()).hexdigest()
    return f'trade:partial:{digest}'


def record_trade(symbol, entry, exit_price, qty, leverage, source, open_time, exit_reason='',
                 signal_type='', market_state_entry='', btc_trend_entry='', breadth_entry='',
                 side='LONG', score=0,
                 atr_entry=0.0, rsi_entry=0.0, funding_entry=0.0,
                 oi_change_entry=0.0, btc_1h_pct=0.0, sl_price=0.0, tp1_price=0.0,
                 margin_mode='', position_alloc_usdt=0.0, account_balance=0.0,
                  pool_remaining=0.0, be_done=0, trail_active=0,
                  algo_sl_id=0, ghost_cleanup=0, position_id='', final_close=True):
    try:
        if float(qty) <= 0:
            return
        if not position_id:
            position_id = f'{source}:{symbol}:{float(entry):.12g}:{float(open_time):.6f}'
        if _is_duplicate_record(symbol, entry, qty, exit_reason):
            return
        if side == 'SHORT':
            _pct = (entry - exit_price) / entry * 100
            _pnl = (entry - exit_price) * qty
        else:
            _pct = (exit_price - entry) / entry * 100
            _pnl = (exit_price - entry) * qty
        pct, pnl_usdt = _pct, _pnl
        formula_pnl = _pnl
        result = 'win' if pct > 0 else 'loss'

        try:
            since = int(open_time * 1000) - 1000
            income_data = []
            page_since = since
            while True:
                batch = fapi_get('/fapi/v1/income', {
                    'symbol': symbol, 'incomeType': 'REALIZED_PNL',
                    'startTime': page_since, 'limit': 100
                })
                if not isinstance(batch, list) or not batch:
                    break
                income_data.extend(batch)
                if len(batch) < 100:
                    break
                page_since = batch[-1]['time'] + 1
            if income_data:
                income_pnl = sum(float(x['income']) for x in income_data)
                # Do not let a zero/empty income response erase a valid
                # price-based PnL calculation.
                if abs(income_pnl) > 1e-9:
                    pnl_usdt = income_pnl
                    result = 'win' if pnl_usdt > 0 else 'loss'
                    notional = entry * qty
                    if notional > 0:
                        pct = pnl_usdt / notional * 100
        except Exception as e:
            pass

        duration_min = int((time.time() - open_time) / 60)

        # Partial fills are accumulated and emitted as one position-level row
        # when the exchange confirms the position is flat.
        if position_id:
            key = _partial_key(position_id)
            partial = _rget(key) or {}
            if not final_close:
                partial['qty'] = float(partial.get('qty', 0)) + float(qty)
                partial['pnl_usdt'] = float(partial.get('pnl_usdt', 0)) + float(formula_pnl)
                partial['exit_notional'] = float(partial.get('exit_notional', 0)) + float(exit_price) * float(qty)
                partial['entry'] = float(entry)
                partial['open_time'] = float(open_time)
                partial['last_exit_reason'] = exit_reason
                _rset(key, partial)
                return
            if partial.get('qty', 0) > 0:
                qty = float(partial['qty']) + float(qty)
                formula_pnl = float(partial.get('pnl_usdt', 0)) + float(formula_pnl)
                exit_notional = float(partial.get('exit_notional', 0)) + float(exit_price) * float(qty - partial['qty'])
                exit_price = exit_notional / qty if qty else exit_price
                pnl_usdt = formula_pnl
                pct = pnl_usdt / (float(entry) * qty) * 100 if entry and qty else 0
                result = 'win' if pnl_usdt > 0 else 'loss'
                duration_min = max(duration_min, int((time.time() - float(partial.get('open_time', open_time))) / 60))
                _rset(key, {})

        _market_state = market_state_entry or ''
        _btc_trend = btc_trend_entry or ''
        _breadth = breadth_entry or ''
        _btc_price = 0.0
        if not _market_state:
            try:
                _md = _rget('market:s3_data')
                if _md:
                    _md_sym = _md.get('symbols', {}).get('BTCUSDT', {})
                    _btc_price = float(_md_sym.get('15m', {}).get('close', _btc_price))
                _ms = _rget('market:s0')
                if _ms:
                    _market_state = str(_ms.get('regime', ''))
                    _btc_trend = str(_ms.get('btc_trend', ''))
                    _breadth = str(_ms.get('breadth', ''))
            except Exception:
                pass
        if result == 'loss':
            _write_loss_cooldown(symbol)
    except Exception:
        return

    try:
        sl_pct_v = round((sl_price - entry) / entry * 100, 2) if entry > 0 and sl_price > 0 else 0.0
        try:
            from shared.binance_api import get_env as _get_data_env
            _env = _get_data_env()
        except Exception:
            _env = 'demo'
        row = json.dumps({
            'symbol': symbol,
            'system_name': source,
            'side': side,
            'entry': entry,
            'exit_price': exit_price,
            'qty': qty,
            'leverage': leverage,
            'pct': round(pct, 2),
            'pnl_usdt': round(pnl_usdt, 2),
            'duration_min': duration_min,
            'result': result,
            'exit_reason': exit_reason,
            'event_type': signal_type,
            'strength': score,
            'margin_mode': margin_mode,
            'position_alloc_usdt': round(float(position_alloc_usdt), 2),
            'account_balance_at_open': round(float(account_balance), 2),
            'pool_remaining_after': round(float(pool_remaining), 2),
            'sl_price': round(float(sl_price), 8),
            'sl_pct': sl_pct_v,
            'atr_entry': round(float(atr_entry), 8),
            'be_done': 1 if be_done else 0,
            'trail_active': 1 if trail_active else 0,
            'market_state': _market_state,
            'btc_trend': _btc_trend,
            'btc_price_close': round(_btc_price, 2),
            'market_breadth': round(float(_breadth), 4) if _breadth and _breadth.replace('.','',1).replace('-','',1).isdigit() else 0,
            'algo_sl_id': int(algo_sl_id) if algo_sl_id else 0,
            'ghost_cleanup': 1 if ghost_cleanup else 0,
            'position_id': str(position_id),
            'env': _env,
        })
        _pg_upsert_trade({
            'position_id': str(position_id),
            'symbol': symbol,
            'system_name': source,
            'side': side,
            'entry_price': float(entry),
            'exit_price': float(exit_price),
            'qty': float(qty),
            'leverage': int(leverage),
            'pnl_pct': float(pct),
            'pnl_usdt': float(pnl_usdt),
            'duration_min': int(duration_min),
            'result': result,
            'exit_reason': exit_reason,
            'event_type': signal_type,
            'strength': float(score),
            'margin_mode': margin_mode,
            'sl_price': float(sl_price),
            'ghost_cleanup': bool(ghost_cleanup),
            'open_time': float(open_time),
            'metadata': {'algo_sl_id': int(algo_sl_id) if algo_sl_id else 0},
            'env': _env,
        })
        _ch_insert('default.trade_history', row)

        # 异步平仓分析（失败不影响记账与交易主链路）
        enqueue_closed_trade({
            'symbol': symbol,
            'source': source,
            'side': side,
            'entry': entry,
            'exit_price': exit_price,
            'qty': qty,
            'leverage': leverage,
            'pct': pct,
            'pnl_usdt': pnl_usdt,
            'duration_min': duration_min,
            'result': result,
            'exit_reason': exit_reason,
            'signal_type': signal_type,
            'score': score,
            'market_state': _market_state,
            'btc_trend': _btc_trend,
            'sl_price': sl_price,
            'open_time': open_time,
            'env': _env,
        })
    except Exception:
        pass

    wins, losses, total_pnl, avg_win, avg_loss = 0, 0, 0.0, 0.0, 0.0
    try:
        _r = _ch_query("SELECT countIf(result='win'), countIf(result='loss'), sum(pnl_usdt), "
                        "avgIf(pct, result='win'), avgIf(pct, result='loss') FROM default.trade_history")
        if _r and _r[0] and len(_r[0]) == 5:
            wins, losses = int(_r[0][0]), int(_r[0][1])
            total_pnl, avg_win, avg_loss = float(_r[0][2]), float(_r[0][3]), float(_r[0][4])
    except Exception:
        pass

    total = wins + losses
    win_rate = wins / total * 100 if total > 0 else 0

    cycle_pnl = get_cycle_pnl()
    cycle_str = f"{cycle_pnl:+.2f} USDT" if cycle_pnl is not None else "计算中..."

    try:
        emoji = '✅' if pct > 0 else '❌'
        msg = (f"{emoji} *平仓* {symbol}\n"
               f"入场: {entry:.4f} → 出场: {exit_price:.4f}\n"
               f"盈亏: {pct:+.1f}% | {pnl_usdt:+.2f} USDT\n"
               f"持仓: {duration_min}分钟 | 杠杆: {leverage}x\n\n"
               f"📊 *累计战绩* ({total}单)\n"
               f"胜率: {win_rate:.0f}% ({wins}胜{losses}负)\n"
               f"本周期净盈亏: {cycle_str}\n"
               f"均盈: {avg_win:+.1f}% | 均亏: {avg_loss:+.1f}%")
        r = requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT_ID, 'text': msg, 'parse_mode': 'Markdown'}, timeout=10)
        mid = r.json().get('result', {}).get('message_id')
        if mid:
            requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/pinChatMessage',
                json={'chat_id': TG_CHAT_ID, 'message_id': mid, 'disable_notification': True}, timeout=5)
    except Exception:
        pass
