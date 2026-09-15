"""PositionMonitoringService — PM 监控编排职责（P7-05B）。

职责（且仅此）：
- monitor_all：监控编排主体（心跳节流 / ghost Step0 序 / filter / per-symbol
  隔离 / ghost 队列消费 / 双 save / summary 条件触发）——从 PM 逐字迁移
- monitor_one：11 步出场链——从 PM 逐字迁移
- ws_on_message：ACCOUNT_UPDATE 解析 + 平仓 record→mark 顺序（逻辑迁入）
- am_leader / ws_connect_loop：ws:leader 租约语义 + connect 门控（逻辑迁入；
  线程 spawn 仍为 pm 入口触发）
- throttle state：process-local（经注入共享 backing，不许 Redis 化）

**不负责**（P7-05B 不迁，仅经注入 callable 调用）：
- _close 生命周期编排（close_fn 注入，指向 PM legacy close path）
- _ghost_cleanup / reconcile_all / external-position adoption 实现（P7-06 真正
  迁移；ghost_cleanup_fn 注入使用）
- WS freshness 语义与 `_load` 三层链（由 pm legacy `_load` 保持 ownership）

依赖方向（禁止反向 import）：
- 不 import shared/position_manager（PM）
- 不 import strategies/shared_executor（SE）
- 不 import position_state / position_ledger / position_protection 具体 service
- 不 import execution/service、risk、s0、s3、s7

import 本包副作用：无（不启线程、不访问 Redis/Binance、不 sleep）。
"""
from __future__ import annotations

import json
from typing import Callable


class PositionMonitoringService:
    """PM 监控职责服务（thin façade；全部依赖经 callable 注入）。

    注入键（P7-05B 约定，见 pm `_monitoring_service()` factory）：
        now / load / save / m1 / gcl / gq / summ
        s6 / log / ghb / shb
        cfg / fund / cls / dc / elm / stag / gate / us / pc / rq
        cts / pt  / pp  / wsl / wsp / swlu / wst
        wcr / trgt / mc / lkey / ltl / inst
        lk / wu / oo / oe / oc
    """

    def __init__(self, *, runtime, state, market, action, ws) -> None:
        self.runtime = runtime
        self.state = state
        self.market = market
        self.action = action
        self.ws = ws

    # ═════════════════════════════════════════════════════════════════
    #  monitor_all（PM 冻结编排逐字迁移；PMB-16/17/19 保持）
    # ═════════════════════════════════════════════════════════════════

    def monitor_all(self, system_filter: str = '') -> list:
        """统一监控所有持仓；返回 [(symbol, reason, close_price), ...]。"""
        positions = self.state.load()
        if not positions:
            now = self.runtime.now()
            if now - self.runtime.ghb() > 60:
                self.runtime.shb(now)
                self.runtime.log('[监控心跳] 无持仓')
            return []
        now = self.runtime.now()
        if now - self.runtime.ghb() > 60:
            self.runtime.shb(now)
            _, _, _, get_price, _, _, _, _ = self.market.s6()
            parts = []
            for s, p in list(positions.items())[:8]:
                entry = p.get('entry')
                side_mark = p.get('side', '?')[:1]
                if entry:
                    try:
                        cur = get_price(s)
                        if cur:
                            pnl = ((entry - cur) / entry * 100
                                   if p.get('side') == 'SHORT'
                                   else (cur - entry) / entry * 100)
                            parts.append(f'{s}({side_mark} {pnl:+.1f}%)')
                        else:
                            parts.append(f'{s}({side_mark})')
                    except Exception:
                        parts.append(f'{s}({side_mark})')
                else:
                    parts.append(f'{s}({side_mark})')
            self.runtime.log(f'[监控心跳] {", ".join(parts)}'
                     if parts else '[监控心跳] 无持仓')

        # ── Step 0: Ghost 清理 — 在 filter 之前全量执行（P7-05A 冻结） ──
        ghost_closed = self.action.gcl(positions, system_filter)
        # 过滤系统
        if system_filter:
            all_positions = positions
            positions = {s: p for s, p in positions.items()
                         if p.get('system', '').startswith(system_filter)}
        else:
            all_positions = positions
        closed = list(ghost_closed)
        if positions:
            # per-symbol isolation：单仓 raise → 后续仓继续（PM 圣经）
            for symbol in list(positions.keys()):
                try:
                    r = self.ws.m1(symbol, positions[symbol], all_positions)
                    if r:
                        closed.append((symbol, *r))
                except Exception as e:
                    self.runtime.log(f'[监控异常] {symbol}: {e}')

        # 消费本轮幽灵仓（AlgoSL 平仓），只消费属于本系统的
        # （PMB-17：len<6 → side=None → filter 失效被消费——冻结语义）
        closed_syms = {c[0] for c in closed}
        remaining = []
        while self.state.gq:
            g = self.state.gq.pop(0)
            # P9-06B（PMB-17 修复）：最小 schema validation —— len≥6 且
            # side 非空；malformed → log + drop（不 requeue、不 infer、
            # empty/no-lend 对象不再炸 monitor loop）
            try:
                _ok = (len(g) >= 6 and g[5])
            except TypeError:
                _ok = False
            if not _ok:
                self.log(f'[ghost queue malformed] {g!r} '
                         '（len<6 或 side 缺失）→ drop')
                continue
            g_sym = g[0]
            g_side = g[5] if len(g) >= 6 else None
            if g_sym in closed_syms:
                continue  # ghost_cleanup 已经处理过了，跳过重复
            if not system_filter or not g_side:
                closed.append(g)
                closed_syms.add(g_sym)
            elif system_filter == 'S6' and g_side == 'LONG':
                closed.append(g)
                closed_syms.add(g_sym)
            elif system_filter == 'S8' and g_side == 'SHORT':
                closed.append(g)
                closed_syms.add(g_sym)
            else:
                remaining.append(g)
        self.state.gq.extend(remaining)
        # PMB-19：终局整量 save（`all_positions`，不过滤）
        self.state.save(all_positions)

        # 持仓快照日志（只在有平仓时）
        if closed:
            self.runtime.summ()

        return closed

    # ═════════════════════════════════════════════════════════════════
    #  monitor_one（11 步出场链逐字迁移；PMB-18/22 保持）
    # ═════════════════════════════════════════════════════════════════

    def monitor_one(self, symbol: str, pos: dict, positions: dict):
        """单币种：硬止损 → be_done → 追踪锁利 → 时间止损（P7-05A 冻结）。

        返回 (reason, price, entry, qty, side) 5-tuple 或 None。
        """
        fapi_get, fapi_post, fapi_delete, get_price, _, _, _, _ = self.market.s6()
        price = get_price(symbol)
        entry = pos['entry']
        atr = pos.get('atr', 0)
        hold = (self.runtime.now() - pos['open_time']) / 60
        cfg = self.market.cfg(pos)

        # 资金费率检查（费率高时主动平仓，避免持续烧钱）
        fund_rate = self.market.fund(symbol)
        if pos['side'] == 'SHORT' and fund_rate < -0.005:
            self.runtime.log(f'[费率警告] {symbol} SHORT 资金费率 '
                     f'{fund_rate:.4%} <-0.5% 强制平仓')
            reason = f'资金费率过高 {fund_rate:.4%}'
            if self.action.cls(symbol, pos, price, reason, positions):
                return (reason, price, entry, pos['qty'], pos['side'])
            return None
        if pos['side'] == 'LONG' and fund_rate > 0.005:
            self.runtime.log(f'[费率警告] {symbol} LONG 资金费率 '
                     f'{fund_rate:.4%} >0.5% 强制平仓')
            reason = f'资金费率过高 {fund_rate:.4%}'
            if self.action.cls(symbol, pos, price, reason, positions):
                return (reason, price, entry, pos['qty'], pos['side'])
            return None
        # 警告级别（仅通知一次）
        warn_tag = 'fund_warned'
        if not pos.get(warn_tag):
            if pos['side'] == 'SHORT' and fund_rate < -0.002:
                pos[warn_tag] = True
                self.runtime.log(f'[费率警告] {symbol} SHORT 资金费率 '
                         f'{fund_rate:.4%} (>=0.2%，注意费率成本)')
            elif pos['side'] == 'LONG' and fund_rate > 0.002:
                pos[warn_tag] = True
                self.runtime.log(f'[费率警告] {symbol} LONG 资金费率 '
                         f'{fund_rate:.4%} (>=0.2%，注意费率成本)')

        if pos['side'] == 'SHORT':
            pnl = (entry - price) / entry * 100
            sl_breached = bool(pos.get('sl')) and pos['sl'] != entry \
                and price >= pos['sl']
        else:
            pnl = (price - entry) / entry * 100
            sl_breached = bool(pos.get('sl')) and pos['sl'] != entry \
                and price <= pos['sl']
        pnl_usdt = ((entry - price) * pos['qty'] if pos['side'] == 'SHORT'
                    else (price - entry) * pos['qty'])

        # 1. 硬止损
        if sl_breached:
            if self.action.cls(symbol, pos, price, '硬止损', positions):
                return ('硬止损', price, entry, pos['qty'], pos['side'])
            return None

        # 2. 紧急止损（主止损 — Binance 已废弃 STOP_MARKET，全靠轮询）
        max_loss = cfg.get('sl_breach_max', -5.0)
        if pnl < max_loss:
            reason = f'紧急止损 pnl={pnl:.1f}%'
            if self.action.cls(symbol, pos, price, reason, positions):
                return (reason, price, entry, pos['qty'], pos['side'])
            return None

        # Keep a short grace period, but do not leave a fast adverse move
        # unprotected for the original 30-minute window.
        if hold >= 5 and pnl <= -2.0:
            try:
                k15 = self.market.dc().get_klines(symbol, '15m', 4)
                if self.action.elm(k15, pos['side']):
                    reason = f'早期亏损保护 pnl={pnl:.1f}%'
                    if self.action.cls(symbol, pos, price, reason, positions):
                        return (reason, price, entry, pos['qty'], pos['side'])
                    return None
            except Exception as e:
                self.runtime.log(f'[早期亏损保护异常] {symbol}: {e}')

        if self.action.stag(pnl_usdt, hold):
            reason = f'低收益停滞 pnl={pnl_usdt:+.2f}U'
            if self.action.cls(symbol, pos, price, reason, positions):
                return (reason, price, entry, pos['qty'], pos['side'])
            return None

        # 3. be_done：盈利达标 → 止损移到成本
        be_pct = cfg.get('be_done_threshold', 2.0)
        if not pos.get('be_done') and pnl >= be_pct:
            self.market.us(symbol, pos, price, entry)

        # 4. 分层止盈：浮盈达到阈值时平掉部分仓位
        partial_tp = cfg.get('partial_tp', {})
        if partial_tp and pnl > 0:
            # 按阈值升序检查（低→高），避免低阈值被高阈值覆盖
            for tp_pct in sorted(partial_tp.keys()):
                if tp_pct <= pnl and tp_pct not in pos.get('tp_done', []):
                    close_ratio = partial_tp[tp_pct]
                    close_qty = self.market.rq(symbol, pos['qty'] * close_ratio)
                    if close_qty > 0 and pos['qty'] > close_qty:
                        pos['tp_done'] = pos.get('tp_done', []) + [tp_pct]
                        self.market.pc(symbol, pos, price, close_qty, tp_pct,
                                positions)
                    break  # 每次只触发一层

        # 5. 追踪锁利
        if pos.get('be_done') and pnl >= be_pct:
            trail_cfg = cfg.get('trail', {'base_mult': 0.3})
            trail_result = self.action.cts(symbol, pos, price, trail_cfg, positions)
            if trail_result == 'exit':
                # 等待区确认：2根连续收>EMA20 → 趋势反转离场
                if self.action.cls(symbol, pos, price,
                            '趋势反转（2次收>EMA20）', positions):
                    return ('趋势反转', price, entry, pos['qty'], pos['side'])
                return None
            elif trail_result is not None:
                # 新追踪价位
                self.action.pt(symbol, pos, trail_result, positions)

        # 5.5 峰值回撤保护：浮盈达标后实时上移锁利止损到交易所，防回踩拉升
        pg_result = self.action.pp(pos, price, cfg)
        if isinstance(pg_result, str):
            if self.action.cls(symbol, pos, price, pg_result, positions):
                return (pg_result, price, entry, pos['qty'], pos['side'])
            return None
        elif pg_result is not None:
            self.action.pt(symbol, pos, pg_result, positions)

        # 6. 1h EMA 安全阀：大周期趋势转向 → 强制离场（但免开仓后前60分钟）
        #      浮盈 >=150% 时豁免，完全交给移动止盈
        if hold < 60 or pnl >= 40:
            pass  # 新开仓60分钟内不介入 / 大盈利仓只靠移动止盈
        else:
            try:
                k1h = self.market.dc().get_klines(symbol, '1h', 22)
                if k1h and len(k1h) >= 21:
                    c1h = [float(x[4]) for x in k1h[-21:]]
                    ema9_1h = sum(c1h[-9:]) / 9
                    ema20_1h = sum(c1h[-20:]) / 20
                    if pos['side'] == 'SHORT' and ema9_1h > ema20_1h * 1.02:
                        if self.action.g1h(pnl):
                            if self.action.cls(symbol, pos, price, '1h趋势反转',
                                        positions):
                                return ('1h趋势反转', price, entry,
                                        pos['qty'], pos['side'])
                        elif not pos.get('trend_reversal_warned'):
                            pos['trend_reversal_warned'] = True
                            self.runtime.log(f'[1h反转观察] {symbol} 当前亏损 '
                                     f'{pnl:+.1f}%，暂不平仓，'
                                     '交给止损/时间止损处理')
                        return None  # PMB-22：阻断当轮 time stop
                    elif pos['side'] != 'SHORT' and ema9_1h < ema20_1h * 0.98:
                        if self.action.g1h(pnl):
                            if self.action.cls(symbol, pos, price, '1h趋势反转',
                                        positions):
                                return ('1h趋势反转', price, entry,
                                        pos['qty'], pos['side'])
                        elif not pos.get('trend_reversal_warned'):
                            pos['trend_reversal_warned'] = True
                            self.runtime.log(f'[1h反转观察] {symbol} 当前亏损 '
                                     f'{pnl:+.1f}%，暂不平仓，'
                                     '交给止损/时间止损处理')
                        return None  # PMB-22：阻断当轮 time stop
            except Exception:
                pass

        # 7. 时间止损
        ts_min = cfg.get('time_stop_min', 240)
        if hold > ts_min:
            if pnl < 0:
                # 浮亏 — 检查是否可延期
                if not pos.get('time_extended'):
                    _, _, _, _, _, get_oi_and_funding, get_rsi, _ = self.market.s6()
                    try:
                        rsi = get_rsi(symbol)
                        _, _, funding = get_oi_and_funding(symbol)
                        ext_r = cfg.get('extend_rsi_min', 60)
                        ext_f = cfg.get('extend_funding_min', 0.0005)
                        if rsi > ext_r and (funding or 0) > ext_f:
                            pos['time_extended'] = True
                            pos['extend_deadline'] = \
                                self.runtime.now() + cfg.get('time_extend_min', 60) * 60
                            self.runtime.log(f'[时间延期] {symbol} RSI={rsi:.0f} '
                                     '再观察1h')
                            return None
                    except Exception:
                        pass
                    if self.runtime.now() < pos.get('extend_deadline', 0):
                        return None
                if self.action.cls(symbol, pos, price, '时间止损', positions):
                    return ('时间止损', price, entry, pos['qty'], pos['side'])
                return None
            elif pnl < be_pct:
                # 微盈/不亏 — 提前释放
                if self.action.cls(symbol, pos, price, '时间止损', positions):
                    return ('时间止损', price, entry, pos['qty'], pos['side'])
                return None

        return None

    # ═════════════════════════════════════════════════════════════════
    #  WS snapshot runtime（逻辑迁入；backing 共享 pm 全局，single owner）
    # ═════════════════════════════════════════════════════════════════

    def ws_on_message(self, ws, message):
        """ACCOUNT_UPDATE 解析（schema/平仓顺序逐字迁移；PMB-21 顺序）。"""
        try:
            data = json.loads(message)
            if data.get('e') != 'ACCOUNT_UPDATE':
                return
            with self.ws.wsl:
                for p in data['a']['P']:
                    sym = p['s']
                    amt = float(p['pa'])
                    if abs(amt) < 0.001:
                        prev = self.ws.wsp.pop(sym, None)
                        if prev and not self.state.wcr(sym):
                            side = prev.get('side', 'LONG')
                            self.runtime.log(f'[WS平仓] {sym} {side} '
                                     f'AlgoSL(入场={prev.get("entry", 0)})')
                            # 先落库（此时 closed 标记未设，不会被
                            # _try_record_ghost_trade 自跳）再标记，
                            # 供 _load/ghost_cleanup 跨进程去重
                            if self.state.trgt(sym, prev):
                                self.mc(sym)
                    else:
                        side = 'LONG' if amt > 0 else 'SHORT'
                        self.ws.wsp[sym] = {
                            'entry': float(p['ep']), 'side': side,
                            'qty': abs(amt),
                            'leverage': int(p.get('lev', 3)),
                            'margin': p.get('mt', 'cross').upper(),
                            'system': '?', 'open_time': self.runtime.now(),
                            'sl': 0, 'be_done': False,
                        }
                self.ws.swlu(self.runtime.now())
        except Exception as e:
            self.runtime.log(f'[WS消息异常] {e}')

    # ═════════════════════════════════════════════════════════════════
    #  leader/lease runtime（fail-open 逐字迁移；PMB-20）
    # ═════════════════════════════════════════════════════════════════

    def am_leader(self) -> bool:
        """尝试成为 WS 领导者：抢锁成功或仍持有锁则返回 True。"""
        try:
            from shared.redis_store import lock_owner, lock_acquire, lock_renew
            owner = lock_owner(self.ws.lkey)
            if owner == self.ws.inst:
                lock_renew(self.ws.lkey, self.ws.inst, self.ws.lttl)
                return True
            if owner is None:
                return lock_acquire(self.ws.lkey, self.ws.inst, self.ws.lttl)
            return False
        except Exception:
            return True  # 兜底：锁服务异常时允许连接，避免完全失去实时监控

    # ═════════════════════════════════════════════════════════════════
    #  connect loop（cadence 逐字迁移；线程 spawn 仍在 pm 入口）
    # ═════════════════════════════════════════════════════════════════

    def ws_connect_loop(self):
        import time
        while not self.ws.wst():
            if not self.ws.ldr():
                time.sleep(5)
                continue
            try:
                import websocket
                key = self.ws.lkfn()
                if not key:
                    time.sleep(5)
                    continue
                url = f'{self.ws.wsf()}/ws/{key}'
                self.runtime.log(f'[WS连接] 用户数据流 {url}')
                ws = websocket.WebSocketApp(
                    url,
                    on_open=self.ws.oofn(),
                    on_message=self.ws_on_message,
                    on_error=self.ws.oe(),
                    on_close=self.ws.oc(),
                )
                ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                self.runtime.log(f'[WS重连] {e}')
            time.sleep(5)

    # ── P8-05B3 兼容 attrs（observable contract 保留；read-only props）──
    @property
    def log(self): return self.runtime.log

    @property
    def now(self): return self.runtime.now

    @property
    def summ(self): return self.runtime.summ

    @property
    def ghb(self): return self.runtime.ghb

    @property
    def shb(self): return self.runtime.shb

    @property
    def load(self): return self.state.load

    @property
    def save(self): return self.state.save

    @property
    def wcr(self): return self.state.wcr

    @property
    def mc(self): return self.state.mc

    @property
    def gq(self): return self.state.gq

    @property
    def trgt(self): return self.state.trgt

    @property
    def fund(self): return self.market.fund

    @property
    def cfg(self): return self.market.cfg

    @property
    def s6(self): return self.market.s6

    @property
    def dc(self): return self.market.dc

    @property
    def us(self): return self.market.us

    @property
    def pc(self): return self.market.pc

    @property
    def rq(self): return self.market.rq

    @property
    def cls(self): return self.action.cls

    @property
    def gcl(self): return self.action.gcl

    @property
    def elm(self): return self.action.elm

    @property
    def stag(self): return self.action.stag

    @property
    def cts(self): return self.action.cts

    @property
    def pt(self): return self.action.pt

    @property
    def pp(self): return self.action.pp

    @property
    def g1h(self): return self.action.g1h

    @property
    def m1(self): return self.ws.m1

    @property
    def wsp(self): return self.ws.wsp

    @property
    def wsl(self): return self.ws.wsl

    @property
    def swlu(self): return self.ws.swlu

    @property
    def wst(self): return self.ws.wst

    @property
    def inst(self): return self.ws.inst

    @property
    def lkey(self): return self.ws.lkey

    @property
    def lttl(self): return self.ws.lttl

    @property
    def ldr(self): return self.ws.ldr

    @property
    def lkfn(self): return self.ws.lkfn

    @property
    def wsf(self): return self.ws.wsf

    @property
    def oofn(self): return self.ws.oofn

    @property
    def oe(self): return self.ws.oe

    @property
    def oc(self): return self.ws.oc
