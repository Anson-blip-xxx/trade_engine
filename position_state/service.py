"""PositionStateService — PM 仓位状态职责独立（P7-02）。

职责（且仅此）：
- Meta load / save（经既有 execution PositionStatePort —— 字段/键序/格式逐字）
- closed marker set / check / clear（S0-9 家族：set 吞错、check 失败 false、
  clear 经 OBS-5 直连 delete callable；无 TTL——ts 比较，`within_hours` 语义）
- 三层 load chain 组装（WS fresh → REST → meta filtered），全部依赖经
  callable 注入（orchestration 的 WS 快照/REST 解析/merge 回调）

**不负责**：open/close/partial 生命周期编排、monitor、reconcile、protection、
ledger、Binance 下单（P7-03+）。

冻结语义（P7-01/P6-01 Golden）：
- fresh-dict-per-call（无缓存/无 defensive copy——identity 序列保持）
- marker 无 TTL；`now-ts < within_hours*3600` 严格比较
- ws_fresh = `last > 0 and now - last < 30`（wall-clock，注入 clock/now）
- REST 异常静默 → meta fallback
- Redis get/set/delete 失败吞错（调用方语义不变）
- dual-writer last-writer-wins（不加 CAS/version/lock）
"""
from __future__ import annotations

import time
from typing import Callable, Optional


class PositionStateService:
    """仓位状态职责服务（stateless façade；可注入 process-local 引用）。"""

    MARKER_PREFIX = 'closed:'

    def __init__(self, *, state_port,
                 marker_set: Callable[..., None],
                 marker_get: Callable[..., Optional[dict]],
                 marker_delete: Callable[[str], None],
                 clock: Optional[Callable[[], float]] = None,
                 sandbox_check: Optional[Callable[[], bool]] = None) -> None:
        # state_port = 既有 execution PositionStatePort（D2 adapter 镜像
        # `_load_meta`/`_save` 全语义）；不另立第二套 port。
        self._port = state_port
        self._marker_set = marker_set
        self._marker_get = marker_get
        self._marker_delete = marker_delete
        self._sandbox_check = sandbox_check or (lambda: False)

    # ── Meta load / save（pm:positions） ─────────────────────────────────

    def load_meta(self) -> dict:
        """pm:positions → fresh dict（PMB-11：每次 JSON 反序列化新对象；
        原地修改不回写）。malformed/missing → {}；Redis/port 失败吞错 → {}
        （P6-01/PM OBS-10 legacy `_load_meta` 的 try/except 语义镜像）。"""
        try:
            return self._port.load_positions()
        except Exception:
            return {}

    def save(self, positions: dict) -> None:
        """整量快照覆盖（no TTL；port 失败吞错——P6-01/PM OBS-10 冻结）。"""
        try:
            self._port.save_positions(positions)
        except Exception:
            pass

    # ── Closed marker（closed:{symbol}） ─────────────────────────────────

    def mark_closed(self, symbol: str) -> None:
        """set：{'ts': now}（无 TTL）——失败吞错。"""
        try:
            self._marker_set(self.MARKER_PREFIX + symbol,
                             {'ts': time.time()})
        except Exception:
            pass

    def was_closed_recently(self, symbol: str,
                            within_hours: int = 4) -> bool:
        """ts 比较窗口（**非 Redis TTL**）：now - ts < within_hours*3600。"""
        try:
            data = self._marker_get(self.MARKER_PREFIX + symbol)
            if data and 'ts' in data:
                return time.time() - data['ts'] < within_hours * 3600
        except Exception:
            pass
        return False

    def clear_closed(self, symbol: str) -> None:
        """OBS-5：经注入的直连 delete callable（不改造为通用 KV；失败吞错）。"""
        try:
            self._marker_delete(self.MARKER_PREFIX + symbol)
        except Exception:
            pass

    # ── 三层 load chain assembly（callable 注入式编排） ──────────────────

    def load_assembly(self, *, ws_snapshot_fn: Callable[[], tuple],
                      rest_positions_fn: Callable[[], dict],
                      merge_and_save_fn: Callable[[dict, float], dict],
                      meta_filtered_fn: Callable[[], dict]) -> dict:
        """WS fresh → REST → meta 严格回退序（P7-01 冻结链）。

        - ws_snapshot_fn() -> (ws_last_update: float, ws_positions: dict)
          （锁/copy 语义由 caller 处理——本服务不持锁）
        - rest_positions_fn() -> dict（解析/异常吞错由 caller 完成）
        - merge_and_save_fn(raw, now) -> merged（meta load+merge enrichment+save）
        - meta_filtered_fn() -> merged（meta load + closed-marker 过滤 + 可选 save）
        """
        now = time.time()
        if self._sandbox_check():
            meta = self.load_meta()
            return {s: p for s, p in meta.items()
                    if not self.was_closed_recently(s)}

        ws_last, ws_positions = ws_snapshot_fn()
        ws_fresh = ws_last > 0 and now - ws_last < 30   # 严格 <30（P7-01 冻结）
        if ws_fresh:
            if ws_positions:
                return merge_and_save_fn(ws_positions, now)

        rest_positions = rest_positions_fn()
        if rest_positions:
            return merge_and_save_fn(rest_positions, now)

        return meta_filtered_fn()
