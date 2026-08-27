"""平仓订单分析（ClickHouse 落库，含 T+15 / T+60 延迟复盘）。"""

import hashlib
import json
import queue
import threading
import time

import requests

from shared.binance_api import FAPI
from shared.clickhouse_client import get_client, insert as _ch_insert, query as _ch_query

_ANALYSIS_TABLE = 'default.trade_analysis'
_ANALYSIS_VERSION = 'v1.1'

_TABLE_READY = False
_WORKER_STARTED = False
_REPLAY_STARTED = False
_WORKER_LOCK = threading.Lock()
_SEQ = 0
_Q = queue.PriorityQueue(maxsize=5000)


def _next_seq() -> int:
    global _SEQ
    _SEQ += 1
    return _SEQ


def _ensure_table():
    global _TABLE_READY
    if _TABLE_READY:
        return
    cli = get_client()
    cli.command(
        """
        CREATE TABLE IF NOT EXISTS default.trade_analysis (
          trade_time DateTime,
          source_trade_hash String,
          symbol String,
          system_name String,
          side String,
          entry Float64,
          exit_price Float64,
          qty Float64,
          leverage Int32,
          pct Float64,
          pnl_usdt Float64,
          duration_min Int32,
          result String,
          exit_reason String,
          event_type String,
          strength Float64,
          market_state String,
          btc_trend String,
          sl_price Float64,
          stop_distance_pct Float64,
          mfe_pct Float64,
          mae_pct Float64,
          exit_efficiency_pct Float64,
          giveback_pct Float64,
          rr_realized Float64,
          quality_score Float64,
          quality_tag String,
          phase String,
          phase_delay_min Int32,
          post_close_return_pct Float64,
          post_close_label String,
          analysis_version String,
          env String DEFAULT 'demo',
          created_at DateTime DEFAULT now()
        )
        ENGINE = MergeTree
        ORDER BY (trade_time, symbol, source_trade_hash)
        """
    )
    cli.command("ALTER TABLE default.trade_analysis ADD COLUMN IF NOT EXISTS phase String DEFAULT 'T0'")
    cli.command("ALTER TABLE default.trade_analysis ADD COLUMN IF NOT EXISTS phase_delay_min Int32 DEFAULT 0")
    cli.command("ALTER TABLE default.trade_analysis ADD COLUMN IF NOT EXISTS post_close_return_pct Float64 DEFAULT 0")
    cli.command("ALTER TABLE default.trade_analysis ADD COLUMN IF NOT EXISTS post_close_label String DEFAULT ''")
    cli.command("ALTER TABLE default.trade_analysis ADD COLUMN IF NOT EXISTS env String DEFAULT 'demo'")
    _TABLE_READY = True


def _calc_mfe_mae(symbol: str, entry: float, side: str, open_time: float):
    if entry <= 0 or open_time <= 0:
        return 0.0, 0.0
    try:
        end_ms = int(time.time() * 1000)
        start_ms = max(0, int(open_time * 1000) - 60_000)
        r = requests.get(
            f'{FAPI}/fapi/v1/klines',
            params={
                'symbol': symbol,
                'interval': '1m',
                'startTime': start_ms,
                'endTime': end_ms,
                'limit': 1500,
            },
            timeout=8,
        )
        data = r.json()
        if not isinstance(data, list) or not data:
            return 0.0, 0.0
        highs = [float(k[2]) for k in data if len(k) > 3]
        lows = [float(k[3]) for k in data if len(k) > 3]
        if not highs or not lows:
            return 0.0, 0.0
        hi, lo = max(highs), min(lows)
        if side == 'SHORT':
            mfe = (entry - lo) / entry * 100
            mae = (entry - hi) / entry * 100
        else:
            mfe = (hi - entry) / entry * 100
            mae = (lo - entry) / entry * 100
        return round(float(mfe), 4), round(float(mae), 4)
    except Exception:
        return 0.0, 0.0


def _quality(pct: float, result: str, exit_reason: str, strength: float, mfe: float, stop_distance_pct: float):
    score = 50.0
    score += 20.0 if result == 'win' else -20.0
    score += max(-20.0, min(20.0, pct * 1.2))
    score += max(-10.0, min(10.0, strength / 10.0))
    if exit_reason in ('time_stop', 'momentum_decay') and result == 'loss':
        score -= 5.0
    if exit_reason in ('stop_loss', 'thesis_failure') and stop_distance_pct > 0:
        score -= 3.0
    if mfe > 0 and pct > 0 and pct < mfe * 0.35:
        score -= 8.0
    score = max(0.0, min(100.0, score))
    if score >= 75:
        tag = 'excellent'
    elif score >= 60:
        tag = 'good'
    elif score >= 45:
        tag = 'neutral'
    elif score >= 30:
        tag = 'weak'
    else:
        tag = 'bad'
    return round(score, 2), tag


def _get_price(symbol: str) -> float:
    try:
        r = requests.get(f'{FAPI}/fapi/v1/ticker/price', params={'symbol': symbol}, timeout=5)
        data = r.json()
        return float(data.get('price', 0))
    except Exception:
        return 0.0


def _post_close_review(side: str, exit_price: float, current_price: float):
    if exit_price <= 0 or current_price <= 0:
        return 0.0, ''
    if side == 'SHORT':
        ret = (exit_price - current_price) / exit_price * 100
    else:
        ret = (current_price - exit_price) / exit_price * 100
    if ret > 1.0:
        label = 'missed_follow_through'
    elif ret < -1.0:
        label = 'avoided_reversal'
    else:
        label = 'neutral_after_exit'
    return round(ret, 4), label


def _analysis_hash(payload: dict, phase: str) -> str:
    base = (
        f"{payload.get('symbol')}|{payload.get('source')}|{payload.get('side')}|"
        f"{payload.get('entry')}|{payload.get('exit_price')}|{payload.get('qty')}|"
        f"{payload.get('open_time', 0)}|{payload.get('exit_reason')}|{phase}"
    )
    return hashlib.sha1(base.encode('utf-8')).hexdigest()


def _analysis_exists(source_trade_hash: str) -> bool:
    try:
        h = source_trade_hash.replace("'", "''")
        rows = _ch_query(f"SELECT count() FROM default.trade_analysis WHERE source_trade_hash='{h}'")
        return bool(rows) and int(rows[0][0]) > 0
    except Exception:
        return False


def analyze_closed_trade(*,
                         symbol: str,
                         source: str,
                         side: str,
                         entry: float,
                         exit_price: float,
                         qty: float,
                         leverage: int,
                         pct: float,
                         pnl_usdt: float,
                         duration_min: int,
                         result: str,
                         exit_reason: str,
                         signal_type: str,
                         score: float,
                         market_state: str,
                         btc_trend: str,
                          sl_price: float,
                          open_time: float,
                          close_ts: float = 0.0,
                          phase: str = 'T0',
                          phase_delay_min: int = 0,
                          env: str = 'demo'):
    _ensure_table()

    payload = {
        'symbol': symbol,
        'source': source,
        'side': side,
        'entry': entry,
        'exit_price': exit_price,
        'qty': qty,
        'open_time': open_time,
        'exit_reason': exit_reason,
    }
    source_trade_hash = _analysis_hash(payload, phase)
    if _analysis_exists(source_trade_hash):
        return

    stop_distance_pct = abs((float(entry) - float(sl_price)) / float(entry) * 100) if float(entry) > 0 and float(sl_price) > 0 else 0.0
    mfe_pct, mae_pct = 0.0, 0.0
    exit_eff, giveback, rr_realized = 0.0, 0.0, 0.0
    quality_score, quality_tag = 0.0, ''
    post_close_return_pct, post_close_label = 0.0, ''

    if phase == 'T0':
        mfe_pct, mae_pct = _calc_mfe_mae(symbol, float(entry), side, float(open_time))
        best = max(0.0, mfe_pct)
        exit_eff = (float(pct) / best * 100.0) if best > 0 else 0.0
        exit_eff = max(0.0, min(150.0, exit_eff))
        giveback = max(0.0, best - max(0.0, float(pct)))
        rr_realized = (float(pct) / stop_distance_pct) if stop_distance_pct > 0 else 0.0
        quality_score, quality_tag = _quality(float(pct), result, exit_reason, float(score), mfe_pct, stop_distance_pct)
    else:
        cur = _get_price(symbol)
        post_close_return_pct, post_close_label = _post_close_review(side, float(exit_price), cur)

    ts = close_ts or time.time()
    row = json.dumps({
        'trade_time': time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(ts)),
        'source_trade_hash': source_trade_hash,
        'symbol': symbol,
        'system_name': source,
        'side': side,
        'entry': float(entry),
        'exit_price': float(exit_price),
        'qty': float(qty),
        'leverage': int(leverage),
        'pct': round(float(pct), 4),
        'pnl_usdt': round(float(pnl_usdt), 4),
        'duration_min': int(duration_min),
        'result': result,
        'exit_reason': exit_reason,
        'event_type': signal_type,
        'strength': round(float(score), 4),
        'market_state': market_state or '',
        'btc_trend': btc_trend or '',
        'sl_price': round(float(sl_price), 8),
        'stop_distance_pct': round(stop_distance_pct, 4),
        'mfe_pct': round(mfe_pct, 4),
        'mae_pct': round(mae_pct, 4),
        'exit_efficiency_pct': round(exit_eff, 4),
        'giveback_pct': round(giveback, 4),
        'rr_realized': round(rr_realized, 4),
        'quality_score': quality_score,
        'quality_tag': quality_tag,
        'phase': phase,
        'phase_delay_min': int(phase_delay_min),
        'post_close_return_pct': round(post_close_return_pct, 4),
        'post_close_label': post_close_label,
        'analysis_version': _ANALYSIS_VERSION,
        'env': env,
    })
    _ch_insert(_ANALYSIS_TABLE, row)


def _queue_phase(payload: dict, phase: str, delay_min: int, run_at: float) -> None:
    item = dict(payload)
    item['phase'] = phase
    item['phase_delay_min'] = delay_min
    item['run_at'] = run_at
    _Q.put_nowait((run_at, _next_seq(), item))


def _worker_loop():
    while True:
        run_at, _, item = _Q.get()
        try:
            now = time.time()
            if run_at > now:
                time.sleep(min(run_at - now, 2.0))
            p = dict(item)
            p.pop('run_at', None)
            analyze_closed_trade(**p)
        except Exception:
            pass
        finally:
            _Q.task_done()


def _replay_loop():
    while True:
        try:
            rows = _ch_query(
                "SELECT symbol, system_name, side, entry, exit_price, qty, leverage, pct, pnl_usdt, "
                "duration_min, result, exit_reason, event_type, strength, market_state, btc_trend, sl_price, "
                "toUnixTimestamp(trade_time), env "
                "FROM default.trade_history "
                "WHERE trade_time >= now() - INTERVAL 2 HOUR "
                "ORDER BY trade_time DESC LIMIT 300"
            )
            now = time.time()
            for r in rows or []:
                if len(r) < 18:
                    continue
                close_ts = float(r[17] or 0)
                if close_ts <= 0:
                    continue
                base = {
                    'symbol': str(r[0]),
                    'source': str(r[1]),
                    'side': str(r[2]),
                    'entry': float(r[3] or 0),
                    'exit_price': float(r[4] or 0),
                    'qty': float(r[5] or 0),
                    'leverage': int(r[6] or 0),
                    'pct': float(r[7] or 0),
                    'pnl_usdt': float(r[8] or 0),
                    'duration_min': int(r[9] or 0),
                    'result': str(r[10]),
                    'exit_reason': str(r[11]),
                    'signal_type': str(r[12]),
                    'score': float(r[13] or 0),
                    'market_state': str(r[14]),
                    'btc_trend': str(r[15]),
                    'sl_price': float(r[16] or 0),
                    'open_time': 0.0,
                    'close_ts': close_ts,
                    'env': str(r[18]) if len(r) > 18 else 'demo',
                }
                for phase, mins in (('T15', 15), ('T60', 60)):
                    if now >= close_ts + mins * 60:
                        h = _analysis_hash(base, phase)
                        if not _analysis_exists(h):
                            _queue_phase(base, phase, mins, now)
        except Exception:
            pass
        time.sleep(60)


def _ensure_worker():
    global _WORKER_STARTED, _REPLAY_STARTED
    with _WORKER_LOCK:
        if not _WORKER_STARTED:
            threading.Thread(target=_worker_loop, name='trade-analyzer', daemon=True).start()
            _WORKER_STARTED = True
        if not _REPLAY_STARTED:
            threading.Thread(target=_replay_loop, name='trade-analyzer-replay', daemon=True).start()
            _REPLAY_STARTED = True


def enqueue_closed_trade(payload: dict) -> bool:
    """平仓后入队：立即分析 + T+15/T+60 延迟复盘。"""
    try:
        _ensure_worker()
        now = time.time()
        p = dict(payload)
        p['close_ts'] = now
        _queue_phase(p, 'T0', 0, now)
        _queue_phase(p, 'T15', 15, now + 15 * 60)
        _queue_phase(p, 'T60', 60, now + 60 * 60)
        return True
    except Exception:
        return False


def _sql_escape(v: str) -> str:
    return str(v or '').replace("'", "''")


def get_rollup_stats(symbol: str = '', event_type: str = '', system_name: str = '', lookback_days: int = 7) -> dict:
    """读取滚动分析统计（默认最近 7 天，T0 + 延迟复盘）。

    只统计当前数据环境（demo/prod），避免 demo 与生产数据互相污染。
    返回结构可直接用于策略过滤与仓位替换打分。
    """
    try:
        _ensure_table()
    except Exception:
        pass

    try:
        from shared.binance_api import get_env
        cur_env = get_env()
    except Exception:
        cur_env = 'demo'

    days = max(1, min(int(lookback_days or 7), 30))
    conds = [f"trade_time >= now() - INTERVAL {days} DAY", f"env='{_sql_escape(cur_env)}'"]
    if symbol:
        conds.append(f"symbol='{_sql_escape(symbol)}'")
    if event_type:
        conds.append(f"event_type='{_sql_escape(event_type)}'")
    if system_name:
        conds.append(f"system_name='{_sql_escape(system_name)}'")
    where = ' AND '.join(conds)

    t0_sql = (
        "SELECT count(), avg(pct), avg(pnl_usdt), avg(rr_realized), avg(quality_score), "
        "countIf(result='win') "
        f"FROM default.trade_analysis WHERE {where} AND phase='T0'"
    )
    post_sql = (
        "SELECT avgIf(post_close_return_pct, phase='T15'), avgIf(post_close_return_pct, phase='T60'), "
        "countIf(phase='T15' AND post_close_label='missed_follow_through'), "
        "countIf(phase='T60' AND post_close_label='missed_follow_through') "
        f"FROM default.trade_analysis WHERE {where}"
    )

    out = {
        'lookback_days': days,
        'symbol': symbol or '*',
        'event_type': event_type or '*',
        'system_name': system_name or '*',
        'trades': 0,
        'win_rate': 0.0,
        'avg_pct': 0.0,
        'avg_pnl_usdt': 0.0,
        'avg_rr_realized': 0.0,
        'avg_quality_score': 0.0,
        't15_avg_post_close_return_pct': 0.0,
        't60_avg_post_close_return_pct': 0.0,
        't15_missed_follow_through': 0,
        't60_missed_follow_through': 0,
    }

    try:
        r = _ch_query(t0_sql)
        if r and r[0] and len(r[0]) >= 6:
            trades = int(r[0][0] or 0)
            wins = int(r[0][5] or 0)
            out['trades'] = trades
            out['win_rate'] = round((wins / trades * 100.0), 2) if trades > 0 else 0.0
            out['avg_pct'] = round(float(r[0][1] or 0), 4)
            out['avg_pnl_usdt'] = round(float(r[0][2] or 0), 4)
            out['avg_rr_realized'] = round(float(r[0][3] or 0), 4)
            out['avg_quality_score'] = round(float(r[0][4] or 0), 4)
    except Exception:
        pass

    try:
        r2 = _ch_query(post_sql)
        if r2 and r2[0] and len(r2[0]) >= 4:
            out['t15_avg_post_close_return_pct'] = round(float(r2[0][0] or 0), 4)
            out['t60_avg_post_close_return_pct'] = round(float(r2[0][1] or 0), 4)
            out['t15_missed_follow_through'] = int(r2[0][2] or 0)
            out['t60_missed_follow_through'] = int(r2[0][3] or 0)
    except Exception:
        pass

    return out
