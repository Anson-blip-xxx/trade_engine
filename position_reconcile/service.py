"""PositionReconcileService — PM 幽灵/对账职责独立（P7-06B）。

职责（且仅此）：
- 通道 A：ghost cleanup（lock → record('手动平仓') → mark → pop → 6-tuple）
- 通道 B：reconcile_all（silent pop/list——无 record/mark/lock）
- exchange-only：external-position alert（30s pending / 24h seen）
- _try_record_ghost_trade（WS record 通道；lock 内二次去重）
- migrate_existing_positions（启动迁移 `state:s6/s8`）

**不拥有 / 不迁**：
- _close lifecycle（无 close_fn 调用——ghost/reconcile 不触发 close）
- ghost queue `_RECENTLY_GHOSTED` consumption（monitoring 侧 ownership，
  backing 仍为 pm 模块全局——不改 monitoring）
- runtime adoption（exchange-only 不建 metadata——启动迁移唯一通道）

依赖方向（AST seal 测试锁定）：
- 不 import shared/position_manager（PM）
- 不 import position_monitoring（MonitoringService）
- 不 import position_state / position_ledger / position_protection 具体 service
- 全部依赖经 callaable 注入（晚绑定）

import 本包副作用：无（不启线程、不访问 Redis/Binance、不 sleep、
不 mutate runtime global）。

冻结语义（P7-06A golden）：
- PMB-17（ghost 队列 side=None filter 失效）不在此实现但契约保持
- PMB-23：cleanup_one **pop 先于 record**；外层大 try 包全环（后继中止）
- PMB-24：reconcile 静默第二通道；无 tradable 过滤
- PMB-25：recently-ghosted dead runtime 不激活
- PMB-26：TG 吞错 / PG 传播；marker 自愈由 merge 链（pm 侧）保持
"""
from __future__ import annotations

class PositionReconcileService:
    """PM 幽灵/对账职责服务（thin façade；全部依赖经 callable 注入）。

    注入键（见 pm `_reconcile_service()` factory 晚绑定约定）：
        lgt(log) / now / load / save / sandbox / exf / gpx
        lacq / lrel / wcr / mc / s6 / posid
        rdget / rdset / rqst(requests module) / tgt / tgc / pg
        sk(SYSTEM_KEYS dict) / pid / uid
    """

    def __init__(self, *, runtime, state, coordination, notification,
                 action) -> None:
        self.runtime = runtime
        self.state = state
        self.coordination = coordination
        self.notification = notification
        self.action = action

    # ═════════════════════════════════════════════════════════════════
    #  通道 A：ghost_cleanup（P7-06A 冻结逐字；PMB-23 保持）
    # ══════════════════    # ── P8-05B2 兼容 attrs（observable contract 保留） ──────────────────
    @property
    def load(self): return self.state.load

    @property
    def save(self): return self.state.save

    @property
    def wcr(self): return self.state.wcr

    @property
    def sk(self): return self.action.sk

    @property
    def lacq(self): return self.coordination.lacq

    @property
    def lrel(self): return self.coordination.lrel

    @property
    def rdget(self): return self.coordination.rdget

    @property
    def rdset(self): return self.coordination.rdset

    @property
    def pid(self): return self.coordination.pid

    @property
    def uid(self): return self.coordination.uid

    @property
    def pg(self): return self.notification.pg

    @property
    def tgt(self): return self.notification.tgt

    @property
    def tgc(self): return self.notification.tgc

    @property
    def rqst(self): return self.notification.rqst

    @property
    def exf(self): return self.action.exf

    @property
    def gpx(self): return self.action.gpx

    @property
    def s6(self): return self.action.s6

    @property
    def mc(self): return self.state.mc

    @property
    def clr(self): return self.state.clr

    @property
    def posid(self): return self.state.posid

    @property
    def rq(self): return self.state.rq

    def ghost_cleanup(self, positions: dict, system_filter: str = '') -> list:
        """幽灵仓清理：对比 Binance positionRisk，清除并记录 trade。
        沙盘模式跳过（行为逐字冻结）。"""
        if self.action.sandbox():
            return []
        closed = []
        try:
            _, _, _, _, _, _, _, record_trade = self.action.s6()
        except Exception:
            record_trade = lambda *a, **kw: None
        try:
            real_r = self.action.exf('/fapi/v2/positionRisk')
            if not isinstance(real_r, list):
                return closed
            real_syms = set()
            for p in real_r:
                if isinstance(p, dict) and \
                        abs(float(p.get('positionAmt', 0))) >= 0.001:
                    real_syms.add(p['symbol'])
            for sym in list(positions.keys()):
                if sym in real_syms:
                    continue
                pos = positions.get(sym)
                if not pos:
                    continue
                # WS 领导者已通过 closed 标记记录过，避免双进程重复记账
                if self.state.wcr(sym):
                    positions.pop(sym, None)
                    continue
                # 只清理属于自己系统的幽灵仓，不碰对方进程的仓位
                if system_filter and not pos.get('system', '') \
                        .startswith(system_filter):
                    continue
                owner = f'ghost-cleanup:{self.coordination.pid()}:{self.coordination.uid()}'
                lock_key = f'pm:ghost_close:{sym}'
                if not self.coordination.lacq(lock_key, owner, ttl=60):
                    continue
                try:
                    self.ghost_cleanup_one(sym, pos, positions,
                                           record_trade, closed)
                finally:
                    self.coordination.lrel(lock_key, owner)
            if closed:
                self.runtime.lgt(f'[幽灵清理完毕] 共清除 {len(closed)} 个幽灵仓')
        except Exception as e:
            self.runtime.lgt(f'[幽灵检测异常] {e}')
        return closed

    def ghost_cleanup_one(self, sym: str, pos: dict, positions: dict,
                          record_trade, closed: list):
        """Remove and record one ghost position while its distributed
        lock is held（PMI-23：pop 先于 record——冻结）。"""
        positions.pop(sym, None)
        entry = pos.get('entry', 0)
        side = pos.get('side', 'LONG')
        qty = pos.get('original_qty', pos.get('qty', 0))
        ghost_price = self.action.gpx(sym) or entry
        self.runtime.lgt(f'[幽灵仓] {sym} 交易所已无持仓，清理 '
                 f'(入场={entry} 现价={ghost_price})')
        record_trade(sym, entry, ghost_price, qty,
                     pos.get('leverage', 1), pos.get('system', ''),
                     pos.get('open_time', self.runtime.now()),
                     exit_reason='手动平仓', side=side,
                     signal_type=pos.get('event_type', ''),
                     score=pos.get('score', 0),
                     atr_entry=pos.get('atr', 0),
                     sl_price=pos.get('sl', 0),
                     margin_mode=pos.get('margin', ''),
                     be_done=pos.get('be_done', False),
                     trail_active=pos.get('trail', False),
                     algo_sl_id=pos.get('algo_sl_id', 0),
                     position_id=self.state.posid(sym, pos), final_close=True,
                     ghost_cleanup=True)
        # 标记已清理，防止下一轮 _load 从 meta 重新读到后再次清理/重复记账
        self.state.mc(sym)
        closed.append((sym, '手动平仓', ghost_price, entry, qty, side))

    # ═════════════════════════════════════════════════════════════════
    #  WS record 通道：_try_record_ghost_trade（lock 内二次去重）
    # ═════════════════════════════════════════════════════════════════

    def try_record_ghost_trade(self, sym: str, meta: dict):
        """幽灵仓数据落库（不抛异常）。通过文件标记去重，防止双进程重复写"""
        owner = f'ghost:{self.coordination.pid()}:{self.coordination.uid()}'
        lock_key = f'pm:ghost_close:{sym}'
        if not self.coordination.lacq(lock_key, owner, ttl=60):
            self.runtime.lgt(f'[幽灵跳过] {sym} 其他进程正在处理平仓')
            return False
        try:
            # 去重：该 symbol 近期已被 _close 处理过则跳过
            if self.state.wcr(sym):
                self.runtime.lgt(f'[幽灵跳过] {sym} 已由 _close 记录，跳过')
                return False

            _, _, _, _, _, _, _, record_trade = self.action.s6()
            entry = meta.get('entry', 0)
            side = meta.get('side', 'LONG')
            qty = meta.get('original_qty', meta.get('qty', 0))
            leverage = meta.get('leverage', 3)
            system_name = meta.get('system', '')
            open_time = meta.get('open_time', self.runtime.now())
            ghost_price = self.action.gpx(sym) or entry
            record_trade(sym, entry, ghost_price, qty, leverage,
                         system_name, open_time,
                         exit_reason='幽灵仓关闭', side=side,
                         signal_type=meta.get('event_type', ''),
                         score=meta.get('score', 0),
                         atr_entry=meta.get('atr', 0),
                         sl_price=meta.get('sl', 0),
                         position_id=self.state.posid(sym, meta),
                         final_close=True, ghost_cleanup=True)
            return True
        except Exception as e:
            self.runtime.lgt(f'[幽灵记录失败] {sym}: {e}')
            return False
        finally:
            self.coordination.lrel(lock_key, owner)

    # ═════════════════════════════════════════════════════════════════
    #  通道 B：reconcile_all（silent —— 无 record/mark/lock，PMB-24）
    # ═════════════════════════════════════════════════════════════════

    def reconcile_all(self):
        """对账：对比 PM state vs Binance 实际持仓（行为逐字冻结）。

        - PM有但Binance无 → 清理幽灵仓（silent：无记账/mark/lock）
        - Binance有但PM无 → 告警日志（missing 列表；无 runtime adoption）
        返回 (ghost_cleaned, missing_tracked)。
        """
        fapi_get, _, _, _, _, _, _, _ = self.action.s6()
        positions = self.state.load()
        ghost = []
        missing = []

        # Binance 实际持仓
        try:
            real_r = fapi_get('/fapi/v2/positionRisk')
            # API错误（限速/banned）时不执行对账——宁漏不错
            if not isinstance(real_r, list):
                self.runtime.lgt(f'[对账跳过] Binance API返回异常: '
                         f'{type(real_r).__name__}')
                return [], []
            real_positions = {}
            for p in real_r:
                if isinstance(p, dict):
                    amt = float(p.get('positionAmt', 0))
                    if abs(amt) >= 0.001:  # 忽略极微量
                        real_positions[p['symbol']] = {
                            'amt': amt,
                            'side': 'SHORT' if amt < 0 else 'LONG',
                        }
        except Exception as e:
            self.runtime.lgt(f'[对账失败] Binance API: {e}')
            return [], []

        # PM有但Binance无
        for sym in list(positions.keys()):
            if sym not in real_positions:
                self.runtime.lgt(f'[对账] 幽灵仓清除: {sym} '
                         f'entry={positions[sym].get("entry")}'
                         '（state有但交易所无）')
                positions.pop(sym, None)
                ghost.append(sym)

        # Binance有但PM无
        for sym, info in real_positions.items():
            if sym not in positions:
                self.runtime.lgt(f'[对账] 漏记仓: {sym} {info["side"]} '
                         f'持仓{info["amt"]}（交易所已有但PM未跟踪）')
                missing.append(sym)

        self.state.save(positions)
        return ghost, missing

    # ═════════════════════════════════════════════════════════════════
    #  External-position alert（30s pending / 24h seen；TG 吞错 PG 传播）
    # ═════════════════════════════════════════════════════════════════

    def notify_external_position(self, symbol: str, raw: dict,
                                 system: str) -> None:
        """Alert once when an exchange position has no local open event
        （per-symbol Redis 键、非原子 dedup——冻结语义）。"""
        grace_sec = 30
        entry = float(raw.get('entry', 0))
        qty = float(raw.get('qty', 0))
        side = raw.get('side', 'LONG')
        fingerprint = f'{side}:{entry:.12g}:{qty:.12g}'
        key = f'alert:external_position:{symbol}'
        pending_key = f'alert:external_position:pending:{symbol}'
        pending = self.coordination.rdget(pending_key) or {}
        if pending.get('fingerprint') != fingerprint:
            self.coordination.rdset(pending_key,
                       {'fingerprint': fingerprint, 'ts': self.runtime.now()})
            return
        if self.runtime.now() - float(pending.get('ts', 0)) < grace_sec:
            return
        seen = self.coordination.rdget(key) or {}
        if seen.get('fingerprint') == fingerprint and \
                self.runtime.now() - float(seen.get('ts', 0)) < 86400:
            return
        self.coordination.rdset(key, {'fingerprint': fingerprint, 'ts': self.runtime.now()})
        self.coordination.rdset(pending_key, {})
        msg = (f'⚠️ 外部/漏记仓位 {symbol}\n'
               f'方向: {side} | 入场: {entry:.8g} | 数量: {qty:.8g}\n'
               f'已纳入 {system} PM 监控，请核对开仓来源。')
        self.runtime.lgt(f'[外部仓位] {symbol} {side} entry={entry} qty={qty} '
                 '未找到本地开仓事件')
        try:
            if self.notification.tgt and self.notification.tgc:
                self.notification.rqst.post(
                    f'https://api.telegram.org/bot{self.notification.tgt}/sendMessage',
                    json={'chat_id': self.notification.tgc, 'text': msg}, timeout=5,
                )
        except Exception:
            pass
        # PG 失败不吞（PMB-26：会向 merge 链传播）
        self.notification.pg({
            'event_id': f'external:{symbol}:{fingerprint}',
            'position_id': f'external:{symbol}:{fingerprint}',
            'event_type': 'EXTERNAL_POSITION_DETECTED',
            'order_id': '', 'fill_id': '', 'price': entry, 'qty': qty,
            'realized_pnl': 0.0, 'payload': {'system': system, 'raw': raw},
        })

    # ═════════════════════════════════════════════════════════════════
    #  migrate_existing_positions（启动 once；不在 runtime adoption）
    # ═════════════════════════════════════════════════════════════════

    def migrate_existing_positions(self):
        """迁移各系统现存持仓到 PM（启动时调用一次）。"""
        positions = self.state.load()
        changed = False
        for system, key in self.action.sk.items():
            try:
                state = self.coordination.rdget(key)
                if not state:
                    continue
                for sym, pos in state.get('positions', {}).items():
                    if sym not in positions:
                        pos['system'] = pos.get('system', system)
                        if 'side' not in pos:
                            pos['side'] = pos.get('side', 'SHORT')
                        if 'original_qty' not in pos:
                            pos['original_qty'] = pos.get('qty', 0)
                        positions[sym] = pos
                        changed = True
                        self.runtime.lgt(f'[迁移] {system} {sym} '
                                 f'入场{pos.get("entry")} 已纳入PM管理')
            except Exception as e:
                self.runtime.lgt(f'[迁移失败] {system}: {e}')
        if changed:
            self.state.save(positions)
        return positions
