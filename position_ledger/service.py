"""PositionLedgerService — PM 平仓账务 settlement 编排（P7-03B）。

职责（且仅此）："被调用后如何记账"：
- duplicate record 判定（经 duplicate_check_fn 注入，SHA1 partial key 经
  partial_key_fn）
- 公式 PnL（LONG=(exit-entry)×qty / SHORT=(entry-exit)×qty）——PMB-7 冻结口径
- income 反算（经 income_fetch_fn 注入 legacy `/fapi/v1/income` 分页——
  P7-00 §4 冻结 hidden IO；zero/empty/malformed/raise → 保留公式值）
- partial 累积（Redis `trade:partial:{sha1}` key 格式冻结；PMB-13 冻结链缺失）
- Episode payload 21 字段 + PG upsert（经 PositionLedgerPort）
- CH row（TradeRecorder trade_history 字面量）经 ch_insert_fn
- analysis enqueue 经 analysis_fn
- TG 通知经 tg_fn（sendMessage+pin 两段在 legacy callable 内）

失败拓扑（P7-03A 冻结，禁止统一）：
- 第一 try（dedup/公式/income/时长/partial merge/market state）异常 → 静默 return
- 第二 try（row build + PG episode + CH row + analysis）→ 任何一步抛错则
  后续（CH/analysis）跳过；**已写入的 PG 不回滚**
- 第三 try（stats/cycle/TG）→ 吞错

无线程/无 queue/无 import 副作用；不依赖 PM/StateService/ExecutionService。
"""
from __future__ import annotations

import json
from typing import Callable, Optional


class PositionLedgerService:
    """平仓 settlement 编排服务（stateless——仅持有注入的 callable）。"""

    def __init__(self, *, ledger_port,
                 duplicate_check_fn: Callable[..., bool],
                 partial_key_fn: Callable[[str], str],
                 income_fetch_fn: Callable[..., Optional[list]],
                 redis_get: Callable[..., Optional[dict]],
                 redis_set: Callable[..., None],
                 loss_cooldown_fn: Callable[[str], None],
                 ch_query_fn: Callable[..., Optional[tuple]],
                 ch_insert_fn: Callable[..., None],
                 analysis_fn: Callable[[dict], None],
                 env_fn: Callable[[], str],
                 tg_fn: Callable[[dict, str], None],
                 time_fn: Callable[[], float]):
        self._ledger_port = ledger_port
        self._dup = duplicate_check_fn
        self._partial_key = partial_key_fn
        self._income_fetch = income_fetch_fn
        self._redis_get = redis_get
        self._redis_set = redis_set
        self._loss_cooldown = loss_cooldown_fn
        self._ch_query = ch_query_fn
        self._ch_insert = ch_insert_fn
        self._analysis_fn = analysis_fn
        self._tg = tg_fn
        self._time = time_fn

    def settle(self, symbol: str, entry: float, exit_price: float, qty: float,
               leverage: int, source: str, open_time: float,
               exit_reason: str = '', signal_type: str = '',
               market_state_entry: str = '', btc_trend_entry: str = '',
               breadth_entry: str = '', side: str = 'LONG', score: int = 0,
               atr_entry: float = 0.0, rsi_entry: float = 0.0,
               funding_entry: float = 0.0, oi_change_entry: float = 0.0,
               btc_1h_pct: float = 0.0, sl_price: float = 0.0,
               tp1_price: float = 0.0, margin_mode: str = '',
               position_alloc_usdt: float = 0.0, account_balance: float = 0.0,
               pool_remaining: float = 0.0, be_done: int = 0,
               trail_active: int = 0, algo_sl_id: int = 0,
               ghost_cleanup: int = 0, position_id: str = '',
               final_close: bool = True) -> None:
        """平仓 settlement（trade_recorder.record_trade 逐字镜像）。"""
        try:
            if float(qty) <= 0:
                return
            if not position_id:
                position_id = (f'{source}:{symbol}:'
                               f'{float(entry):.12g}:{float(open_time):.6f}')
            if self._dup(symbol, entry, qty, exit_reason):
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
                    batch = self._income_fetch(
                        '/fapi/v1/income', {'symbol': symbol,
                                            'incomeType': 'REALIZED_PNL',
                                            'startTime': page_since,
                                            'limit': 100})
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
            except Exception:
                pass

            duration_min = int((self._time() - open_time) / 60)

            # Partial fills are accumulated and emitted as one position-level
            # row when the exchange confirms the position is flat.
            if position_id:
                key = self._partial_key(position_id)
                partial = self._redis_get(key) or {}
                if not final_close:
                    partial['qty'] = float(partial.get('qty', 0)) + float(qty)
                    partial['pnl_usdt'] = (float(partial.get('pnl_usdt', 0))
                                           + float(formula_pnl))
                    partial['exit_notional'] = (float(partial.get('exit_notional', 0))
                                                + float(exit_price) * float(qty))
                    partial['entry'] = float(entry)
                    partial['open_time'] = float(open_time)
                    partial['last_exit_reason'] = exit_reason
                    self._redis_set(key, partial)
                    return
                if partial.get('qty', 0) > 0:
                    qty = float(partial['qty']) + float(qty)
                    formula_pnl = (float(partial.get('pnl_usdt', 0))
                                   + float(formula_pnl))
                    exit_notional = (float(partial.get('exit_notional', 0))
                                     + float(exit_price)
                                     * float(qty - partial['qty']))
                    exit_price = exit_notional / qty if qty else exit_price
                    pnl_usdt = formula_pnl
                    pct = (pnl_usdt / (float(entry) * qty) * 100
                           if entry and qty else 0)
                    result = 'win' if pnl_usdt > 0 else 'loss'
                    duration_min = max(
                        duration_min,
                        int((self._time()
                             - float(partial.get('open_time', open_time)))
                            / 60))
                    self._redis_set(key, {})

            _market_state = market_state_entry
            _btc_trend = btc_trend_entry
            _breadth = breadth_entry
            _btc_price = 0.0
            if not _market_state:
                try:
                    _md = self._redis_get('market:s3_data')
                    if _md:
                        _btc_price = float(
                            _md['symbols']['BTCUSDT']['15m']['close'])
                except Exception:
                    pass
                try:
                    _ms = self._redis_get('market:s0')
                    if _ms:
                        _market_state = str(_ms.get('regime', ''))
                        _btc_trend = str(_ms.get('btc_trend', ''))
                        _breadth = str(_ms.get('breadth', ''))
                except Exception:
                    pass
            if result == 'loss':
                self._loss_cooldown(symbol)
        except Exception:
            return

        try:
            sl_pct_v = (round((sl_price - entry) / entry * 100, 2)
                        if entry > 0 and sl_price > 0 else 0.0)
            _env = 'demo'
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
                'market_breadth': (round(float(_breadth), 4)
                                   if _breadth is not None and
                                   _breadth.replace('.', '', 1).replace('-', '', 1)
                                   .isdigit() else 0),
                'algo_sl_id': int(algo_sl_id) if algo_sl_id else 0,
                'ghost_cleanup': 1 if ghost_cleanup else 0,
                'position_id': str(position_id),
                'env': _env,
            })
            self._ledger_port.upsert_trade_episode({
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
                'metadata': {
                    'algo_sl_id': int(algo_sl_id) if algo_sl_id else 0},
                'env': _env,
            })
            self._ch_insert('default.trade_history', row)

            # 异步平仓分析（失败不影响记账与交易主链路）
            self._dispatch_analysis(symbol=symbol, source=source, side=side,
                           entry=entry, exit_price=exit_price, qty=qty,
                           leverage=leverage, pct=pct, pnl_usdt=pnl_usdt,
                           duration_min=duration_min, result=result,
                           exit_reason=exit_reason, signal_type=signal_type,
                           score=score, market_state=_market_state,
                           btc_trend=_btc_trend, sl_price=sl_price,
                           open_time=open_time, env=_env)
        except Exception:
            pass

        try:
            wins, losses, total_pnl, avg_win, avg_loss = 0, 0, 0.0, 0.0, 0.0
            try:
                _r = self._ch_query("SELECT countIf(result='win'), "
                                    "countIf(result='loss'), sum(pnl_usdt), "
                                    "avgIf(pct, result='win'), "
                                    "avgIf(pct, result='loss') "
                                    "FROM default.trade_history")
                if _r and _r[0] and len(_r[0]) == 5:
                    (wins, losses, total_pnl, avg_win,
                     avg_loss) = (int(_r[0][0]), int(_r[0][1]),
                                  float(_r[0][2]), float(_r[0][3]),
                                  float(_r[0][4]))
            except Exception:
                pass

            total = wins + losses
            win_rate = wins / total * 100 if total > 0 else 0

            cycle_pnl = self._cycle_pnl()
            cycle_str = (f"{cycle_pnl:+.2f} USDT"
                         if cycle_pnl is not None else "计算中...")

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
                self._tg(msg)
            except Exception:
                pass
        except Exception:
            pass

    # ── 内嵌 helper（行为逐字；callables 经构造注入） ────────────────────

    def _cycle_pnl(self) -> Optional[float]:
        try:
            checkpoint = self._redis_get('checkpoint:pnl')
            if not checkpoint:
                checkpoint = {'start_ms': int(self._time() * 1000)}
                self._redis_set('checkpoint:pnl', checkpoint)
            start_ms = checkpoint['start_ms']
            total = 0
            for itype in ['REALIZED_PNL', 'FUNDING_FEE', 'COMMISSION']:
                data = self._income_fetch('/fapi/v1/income',
                                          {'incomeType': itype,
                                           'startTime': start_ms,
                                           'limit': 1000})
                if isinstance(data, list):
                    total += sum(float(x['income']) for x in data)
            return round(total, 4)
        except Exception:
            return None

    def _dispatch_analysis(self, symbol: str, source: str, side: str,
                           entry: float, exit_price: float, qty: float,
                           leverage: int, pct: float, pnl_usdt: float,
                           duration_min: int, result: str,
                           exit_reason: str, signal_type: str, score: int,
                           market_state: str, btc_trend: str,
                           sl_price: float, open_time: float,
                           env: str) -> None:
        """analysis（经注入 callable——legacy `enqueue_closed_trade` 语义）。"""
        try:
            self._analysis_fn({
                'symbol': symbol, 'source': source, 'side': side,
                'entry': entry, 'exit_price': exit_price, 'qty': qty,
                'leverage': leverage, 'pct': pct, 'pnl_usdt': pnl_usdt,
                'duration_min': duration_min, 'result': result,
                'exit_reason': exit_reason, 'signal_type': signal_type,
                'score': score, 'market_state': market_state,
                'btc_trend': btc_trend, 'sl_price': sl_price,
                'open_time': open_time, 'env': env,
            })
        except Exception:
            pass
