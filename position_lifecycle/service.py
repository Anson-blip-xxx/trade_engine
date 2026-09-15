"""PositionLifecycleService — PM helmet 开/平/部分平生命周期职责独立（P7-07B）。

职责（且仅此）：
- open_position：guard → 杠杆 → 保证金 → MARKET order → AlgoSL enqueue →
  dup check → save → True/False（逐字；OBS-3/PMB-30 序不变）
- close_position → _close（force 语义；marker-first PMB-27；save 首末位
  不对称 PMB-28；不 cancel 的 code-fail 保护——逐字）
- _partial_close（无 reduceOnly / 负数增仓 / 0 ledger —— 逐字）

**不拥有**：
- 你的 `_close` execution intent 构造/交易所端口（经 exec_fn/注入）ok
- WS/reconcile 调用方（只依赖本项目 legacy 签名）

backing：`_CLOSE_ERROR_LOG_TS` 平仓错误节流仍是 pm 模块全局（单 backing 注入）。
依赖方向：零反向 import（AST seal锁定）——PM/monitoring/reconcile/state/
ledger/protection 均**不** service 进本包。
"""
from __future__ import annotations


class PositionLifecycleService:
    """PM helmet 职责服务（thin façade；5 责任域 bundle 注入，data-only holder）。"""

    def __init__(self, *, runtime, execution, state, protection,
                 action) -> None:
        self.runtime = runtime
        self.execution = execution
        self.state = state
        self.protection = protection
        self.action = action

    # ═════════════════════════════════════════════════════════════════
    #  open（逐字迁移）
    # ═════════════════════════════════════════════════════════════════

    def open_position(self, symbol: str, side: str, entry: float, qty: float,
                      leverage: int, sl: float, tp1: float = 0,
                      tp2: float = 0, *, atr: float = 0, score: int = 0,
                      reasons: dict = None, signal_type: str = '',
                      system: str = '', margin_type: str = 'CROSSED',
                      metadata: dict = None) -> bool:
        """
        统一开仓。
        - 设杠杆 / 保证金
        - 下市价单
        - 挂条件止损单 Algo Order API（/fapi/v1/algoOrder）
        - 写入 pm_state + 同步原系统
        """
        if self.state.wcr(symbol):
            self.runtime.log(f'[开仓拒绝] {symbol} 4h 内被平仓过，跳过')
            return False
        _, fapi_post, _, _, _, _, _, _ = self.action.s6()

        # 0. 本地重复 precheck（T1-A，P9-03B：在真实 side effect 之前拒绝；
        #    duplicate return 合同不变 = True；key 仍 symbol-only）
        positions = self.state.load()
        if symbol in positions:
            self.runtime.log(f'[开仓] {symbol} 已在持仓中，跳过')
            return True

        # 1. 杠杆
        try:
            fapi_post('/fapi/v1/leverage',
                      {'symbol': symbol, 'leverage': leverage})
        except Exception as e:
            self.runtime.log(f'[开仓] {symbol} 杠杆设置: {e}')

        # 2. 保证金模式
        try:
            fapi_post('/fapi/v1/marginType',
                      {'symbol': symbol, 'marginType': margin_type})
        except Exception as e:
            self.runtime.log(f'[开仓] {symbol} 保证金({margin_type}): {e}')

        # 3. 市价开仓
        order_side = 'SELL' if side == 'SHORT' else 'BUY'
        try:
            order = fapi_post('/fapi/v1/order', {
                'symbol': symbol, 'side': order_side, 'type': 'MARKET',
                'quantity': qty, 'positionSide': 'BOTH',
            })
        except Exception as e:
            self.runtime.log(f'[开仓失败] {symbol}: {e}')
            return False

        # 4. 下条件止损单（Algo Order API）→ 入队异步消费
        sl_side = 'BUY' if side == 'SHORT' else 'SELL'
        self.protection.wkr()
        self.protection.enq(symbol, sl_side, sl, qty)
        self.runtime.log(f'[开仓AlgoSL] {symbol} 止损{sl} 已入队')
        algo_sl_id = None  # worker 完成后会更新 Redis

        # 5. 记录
        now = int(self.runtime.now())
        position = {
            'entry': entry, 'qty': qty, 'original_qty': qty,
            'leverage': leverage, 'sl': sl,
            'open_time': now, 'atr': atr,
            'side': side, 'system': system,
            'signal_type': signal_type, 'score': score,
            'margin_type': margin_type,
            'be_done': False,
            'tp_done': [],
            'highest': entry if side != 'SHORT' else entry,
            'lowest': entry if side == 'SHORT' else entry,
            'algo_sl_id': algo_sl_id,
            **(metadata or {}),
            **(reasons or {}),
        }
        positions[symbol] = position
        self.state.save(positions)
        self.runtime.log(f'[开仓] {system} {symbol} {side} 入场{entry} 止损{sl} '
                 f'{leverage}x score={score}')
        return True

    # ═════════════════════════════════════════════════════════════════
    #  close（marker-first / save 序不对称，逐字）
    # ═════════════════════════════════════════════════════════════════

    def close_position(self, symbol: str, reason: str) -> bool:
        """外部调用平仓（经注入 close_fn 保留 pm seam）。"""
        positions = self.state.load()
        pos = positions.get(symbol)
        if not pos:
            self.runtime.log(f'[平仓] {symbol} PM无此持仓')
            return False
        _, _, _, get_price, _, _, _, _ = self.action.s6()
        price = get_price(symbol)
        return self.action.close_fn(symbol, pos, price, reason, positions,
                             force=True)

    def close(self, symbol: str, pos: dict, price: float, reason: str,
              positions: dict, *,
              force: bool = False) -> bool:
        """内部平仓：取消条件单 → 确认实盘 → 市价平 → 落库 → 删记录。

        force=False 时防重入：该币 4h 内已被处理过则直接跳过。"""
        if not force and self.state.wcr(symbol):
            self.runtime.log(f'[平仓跳过] {symbol} 近期已处理，防止重复平仓 ({reason})')
            return False
        self.state.mc(symbol)
        _, fapi_post, _, _, _, _, _, record_trade = self.action.s6()

        # ═══ 沙盘模式：跳过 Binance API 检查，直接记录 ═══
        if self.action.sandbox():
            try:
                from scripts.sandbox import _close_position as _sb_close
                _sb_close(symbol)
            except Exception:
                pass
            self.runtime.log(f'[平仓·沙盘] {symbol} {reason} '
                     f'入场={pos.get("entry")} 现价={price}')
            close_qty = pos.get('original_qty', pos.get('qty', 0))
            if pos['side'] == 'SHORT':
                pnl_pct = (pos['entry'] - price) / pos['entry'] * 100
                pnl_u = round((pos['entry'] - price) * close_qty, 2)
            else:
                pnl_pct = (price - pos['entry']) / pos['entry'] * 100
                pnl_u = round((price - pos['entry']) * close_qty, 2)
            record_trade(symbol, pos['entry'], price, close_qty,
                         pos.get("leverage", 3),
                         pos.get('system', ''), pos['open_time'],
                         exit_reason=reason, side=pos['side'],
                         signal_type=pos.get('event_type', ''),
                         score=pos.get('score', 0),
                         atr_entry=pos.get('atr', 0),
                         sl_price=pos.get('sl', 0),
                         margin_mode=pos.get('margin', ''),
                         be_done=pos.get('be_done', False),
                         trail_active=pos.get('trail', False),
                         algo_sl_id=pos.get('algo_sl_id', 0),
                         position_id=self.state.posid(symbol, pos),
                         final_close=True)
            positions.pop(symbol, None)
            self.state.save(positions)
            return True

        # ═══ 实盘模式 ═══
        fapi_get, _, _, _, _, _, _, record_trade = self.action.s6()

        try:
            real_r = fapi_get('/fapi/v2/positionRisk', {'symbol': symbol})
            if pos['side'] == 'SHORT':
                real_pos = next((x for x in real_r if isinstance(x, dict)
                                 and x.get('symbol') == symbol
                                 and float(x.get('positionAmt', 0)) < 0),
                                None)
            else:
                real_pos = next((x for x in real_r if isinstance(x, dict)
                                 and x.get('symbol') == symbol
                                 and float(x.get('positionAmt', 0)) > 0),
                                None)
            if not real_pos:
                # 止损单已在交易所触发平仓，记录本次平仓
                try:
                    self.protection.cxa(symbol)
                    self.runtime.log(f'[平仓Algo取消] {symbol} 已清理全部条件单')
                except Exception as e:
                    self.runtime.log(f'[平仓Algo取消异常] {symbol}: {e}')
                close_qty = pos.get('original_qty', pos.get('qty', 0))
                if pos['side'] == 'SHORT':
                    pnl_pct = (pos['entry'] - price) / pos['entry'] * 100
                    pnl_u = round((pos['entry'] - price) * close_qty, 2)
                else:
                    pnl_pct = (price - pos['entry']) / pos['entry'] * 100
                    pnl_u = round((price - pos['entry']) * close_qty, 2)
                self.runtime.log(f'[平仓] {symbol} 交易所已平仓 pnl={pnl_pct:+.1f}% '
                         f'({pnl_u:+.2f}U) 原因={reason}')
                record_trade(symbol, pos['entry'], price, close_qty,
                             pos.get("leverage", 3),
                             pos.get('system', ''), pos['open_time'],
                             exit_reason=reason, side=pos['side'],
                             signal_type=pos.get('event_type', ''),
                             score=pos.get('score', 0),
                             atr_entry=pos.get('atr', 0),
                             sl_price=pos.get('sl', 0),
                             margin_mode=pos.get('margin', ''),
                             be_done=pos.get('be_done', False),
                             trail_active=pos.get('trail', False),
                             algo_sl_id=pos.get('algo_sl_id', 0),
                             position_id=self.state.posid(symbol, pos),
                             final_close=True)
                self.action.pg({
                    'event_id': f"position:{self.state.posid(symbol, pos)}:flat",
                    'position_id': self.state.posid(symbol, pos),
                    'event_type': 'EXCHANGE_POSITION_FLAT',
                    'order_id': '', 'fill_id': '', 'price': price,
                    'qty': close_qty, 'realized_pnl': pnl_u,
                    'payload': {'reason': reason},
                })
                positions.pop(symbol, None)
                self.state.save(positions)
                return True

            requested_close_qty = self.state.rq(
                symbol, abs(float(real_pos['positionAmt'])))
            close_qty = requested_close_qty
            # P4-03-01-C：订单执行经 ExecutionService → Binance Port
            #（intent 由 Core 构造：SHORT→BUY / 其余→SELL，MARKET + BOTH +
            # reduceOnly='true' —— E-OBS-5 冻结）。
            result = self.execution.exec_fn().execute_order(
                self.execution.ci(symbol, pos['side'], close_qty)).raw
            if isinstance(result, dict) and result.get('code'):
                self.action.lce(symbol, result.get('msg', result), interval=60)
                # 不要在市价单失败前删除原止损单，避免仓位裸奔。
                self.state.clr(symbol)
                return False

            # Market orders can be partially filled. Keep the position and
            # accumulate this slice until positionRisk confirms it is flat.
            reported_filled_qty = abs(float(result.get('executedQty', 0))) \
                if isinstance(result, dict) else 0.0
            remaining_r = fapi_get('/fapi/v2/positionRisk', {'symbol': symbol})
            remaining_pos = next(
                (x for x in (remaining_r or []) if isinstance(x, dict)
                 and x.get('symbol') == symbol
                 and ((pos['side'] == 'SHORT'
                       and float(x.get('positionAmt', 0)) < 0)
                      or (pos['side'] != 'SHORT'
                          and float(x.get('positionAmt', 0)) > 0))),
                None)
            remaining_qty = abs(float(remaining_pos.get('positionAmt', 0))) \
                if remaining_pos else 0.0
            if remaining_qty >= 0.001:
                if reported_filled_qty < 0.001:
                    pos['qty'] = remaining_qty
                    positions[symbol] = pos
                    self.state.save(positions)
                    self.action.lce(symbol, '平仓响应无成交数量，保留仓位等待重试')
                    return False
                filled_qty = reported_filled_qty
                pos['qty'] = remaining_qty
                positions[symbol] = pos
                self.state.save(positions)
                record_trade(symbol, pos['entry'], price, filled_qty,
                             pos.get("leverage", 3),
                             pos.get('system', ''), pos['open_time'],
                             exit_reason=reason, side=pos['side'],
                             signal_type=pos.get('event_type', ''),
                             score=pos.get('score', 0),
                             atr_entry=pos.get('atr', 0),
                             sl_price=pos.get('sl', 0),
                             margin_mode=pos.get('margin', ''),
                             be_done=pos.get('be_done', False),
                             trail_active=pos.get('trail', False),
                             algo_sl_id=pos.get('algo_sl_id', 0),
                             position_id=self.state.posid(symbol, pos),
                             final_close=False)
                self.action.pg({
                    'event_id': (f"order:{result.get('orderId', '')}:close:"
                                 f"{filled_qty}"),
                    'position_id': self.state.posid(symbol, pos),
                    'event_type': 'CLOSE_ORDER_PARTIAL',
                    'order_id': str(result.get('orderId', '')), 'fill_id': '',
                    'price': price, 'qty': filled_qty, 'realized_pnl': 0.0,
                    'payload': {**result,
                                'exchange_price': result.get('price', '0'),
                                'price': price, 'accounted_qty': filled_qty,
                                'execution_status': result.get('status', '')},
                })
                self.runtime.log(f'[平仓部分成交] {symbol} qty={filled_qty} '
                         f'剩余={remaining_qty}')
                return False
            # Some Binance-compatible responses omit executedQty on a filled
            # market order. If positionRisk is flat, the requested quantity is
            # the only safe fallback for accounting.
            close_qty = (reported_filled_qty
                         if reported_filled_qty >= 0.001
                         else requested_close_qty)
        except Exception as e:
            self.runtime.log(f'[平仓异常] {symbol}: {e}')
            self.state.clr(symbol)
            return False

        # 市价平仓成功后再清理剩余条件单。
        try:
            self.protection.cxa(symbol)
            self.runtime.log(f'[平仓Algo取消] {symbol} 已清理全部条件单')
        except Exception as e:
            self.runtime.log(f'[平仓Algo取消异常] {symbol}: {e}')

        # 盈亏
        if pos['side'] == 'SHORT':
            pnl_pct = (pos['entry'] - price) / pos['entry'] * 100
            pnl_u = round((pos['entry'] - price) * close_qty, 2)
        else:
            pnl_pct = (price - pos['entry']) / pos['entry'] * 100
            pnl_u = round((price - pos['entry']) * close_qty, 2)

        close_payload = dict(result)
        close_payload['exchange_price'] = close_payload.get('price', '0')
        close_payload['price'] = price
        close_payload['accounted_qty'] = close_qty
        close_payload['execution_status'] = close_payload.get('status', '')
        self.action.pg({
            'event_id': f"order:{result.get('orderId', '')}:close:final",
            'position_id': self.state.posid(symbol, pos),
            'event_type': 'CLOSE_ORDER_FILLED',
            'order_id': str(result.get('orderId', '')), 'fill_id': '',
            'price': price, 'qty': close_qty, 'realized_pnl': pnl_u,
            'payload': close_payload,
        })

        self.runtime.log(f'[平仓] {symbol} {reason} pnl={pnl_pct:+.1f}% ({pnl_u:+.2f}U)')

        # 落库
        record_trade(symbol, pos['entry'], price, close_qty,
                     pos.get("leverage", 3),
                     pos.get('system', ''), pos['open_time'],
                     exit_reason=reason, side=pos['side'],
                     signal_type=pos.get('event_type', ''),
                     score=pos.get('score', 0),
                     atr_entry=pos.get('atr', 0),
                     sl_price=pos.get('sl', 0),
                     margin_mode=pos.get('margin', ''),
                     be_done=pos.get('be_done', False),
                     trail_active=pos.get('trail', False),
                     algo_sl_id=pos.get('algo_sl_id', 0),
                     position_id=self.state.posid(symbol, pos),
                     final_close=True)

        # 删记录
        positions.pop(symbol, None)
        self.state.save(positions)
        return True

    # ═════════════════════════════════════════════════════════════════
    #  partial close（逐字：无 reduceOnly / 负数增仓 / 0 ledger）
    # ═════════════════════════════════════════════════════════════════

    def partial_close(self, symbol: str, pos: dict, price: float,
                      close_qty: float, tp_pct: float, positions: dict):
        """分层止盈：市价平掉 close_qty 数量，保留剩余仓位。"""
        try:
            r = self.execution.exec_fn().execute_order(
                self.execution.pi(symbol, pos['side'], close_qty)).raw
            if not isinstance(r, dict) or r.get('code') is not None:
                self.runtime.log(f'[分层止盈失败] {symbol} qty={close_qty}: '
                         f'交易所拒绝 {r}')
                return
        except Exception as e:
            self.runtime.log(f'[分层止盈失败] {symbol} qty={close_qty}: {e}')
            return
        pos['qty'] = round(pos['qty'] - close_qty, 4)
        pnl_u = round((price - pos['entry']) * close_qty, 2) \
            if pos['side'] == 'LONG' \
            else round((pos['entry'] - price) * close_qty, 2)
        self.state.save(positions)
        self.runtime.log(f'[分层止盈] {symbol} +{pnl_u:.2f}USDT qty={close_qty} '
                 f'剩余={pos["qty"]} ({tp_pct}%)')
