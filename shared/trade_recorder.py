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


def _is_duplicate_record(symbol, entry, qty, exit_reason):
    """双进程重复记账防御：2 分钟内已有完全相同的平仓记录则跳过（P7-01 冻结）。
    查 CH trade_history 同 sym/entry/exit/qty + 2 分钟 window。"""
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
    """partial 累积 redis key（P7-03A 冻结格式 trade:partial:{sha1}）。"""
    import hashlib
    return f"trade:partial:{hashlib.sha1(position_id.encode()).hexdigest()}"


def _send_and_pin(msg: str):
    """legacy TG helper（P7-01 冻结：sendMessage→mid→pin，吞错）。"""
    import requests as _r
    r = _r.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
                json={'chat_id': TG_CHAT_ID, 'text': msg,
                      'parse_mode': 'Markdown'}, timeout=10)
    mid = r.json().get('result', {}).get('message_id')
    if mid:
        _r.post(f'https://api.telegram.org/bot{TG_TOKEN}/pinChatMessage',
                json={'chat_id': TG_CHAT_ID, 'message_id': mid,
                      'disable_notification': True}, timeout=5)


def record_trade(symbol, entry, exit_price, qty, leverage, source, open_time,
                 exit_reason='', signal_type='', market_state_entry='',
                 btc_trend_entry='', breadth_entry='', side='LONG', score=0,
                 atr_entry=0.0, rsi_entry=0.0, funding_entry=0.0,
                 oi_change_entry=0.0, btc_1h_pct=0.0, sl_price=0.0,
                 tp1_price=0.0, margin_mode='', position_alloc_usdt=0.0,
                 account_balance=0.0, pool_remaining=0.0, be_done=0,
                 trail_active=0, algo_sl_id=0, ghost_cleanup=0,
                 position_id='', final_close=True):
    """交易记录（P7-03B：settlement 编排委托 PositionLedgerService；
    IO via callables 晚绑定注入，行为/PnL/失败拓扑逐字冻结）。"""
    def income_fetch(path, params=None):
        return fapi_get(path, params or {})
    def duplicate_check(sym, ent, qty_, reason):
        return _is_duplicate_record(sym, ent, qty_, reason)
    def partial_key(pid):
        return _partial_key(pid)
    def loss_cooldown(sym):
        return _write_loss_cooldown(sym)

    class _PortShim:
        def upsert_trade_episode(self, data):
            _pg_upsert_trade(data)

    from position_ledger.service import PositionLedgerService as _svc
    return _svc(
        ledger_port=_PortShim(),
        duplicate_check_fn=duplicate_check,
        partial_key_fn=partial_key,
        income_fetch_fn=income_fetch,
        redis_get=_rget,
        redis_set=_rset,
        loss_cooldown_fn=loss_cooldown,
        ch_query_fn=lambda sql: _ch_query(sql),
        ch_insert_fn=lambda t, r: _ch_insert(t, r),
        analysis_fn=enqueue_closed_trade,
        env_fn=lambda: None,
        tg_fn=_send_and_pin,
        time_fn=lambda: time.time(),
    ).settle(symbol=symbol, entry=entry, exit_price=exit_price, qty=qty,
             leverage=leverage, source=source, open_time=open_time,
             exit_reason=exit_reason, signal_type=signal_type,
             market_state_entry=market_state_entry,
             btc_trend_entry=btc_trend_entry, breadth_entry=breadth_entry,
             side=side, score=score, atr_entry=atr_entry,
             rsi_entry=rsi_entry, funding_entry=funding_entry,
             oi_change_entry=oi_change_entry, btc_1h_pct=btc_1h_pct,
             sl_price=sl_price, tp1_price=tp1_price,
             margin_mode=margin_mode,
             position_alloc_usdt=position_alloc_usdt,
             account_balance=account_balance,
             pool_remaining=pool_remaining, be_done=be_done,
             trail_active=trail_active, algo_sl_id=algo_sl_id,
             ghost_cleanup=ghost_cleanup, position_id=position_id,
             final_close=final_close)
