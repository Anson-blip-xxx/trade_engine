"""MonitoringService dependency bundles（P8-05B3；data-only holder）。

leaf module（禁 import PM/Execution concrete 等）；
bundle 由 pm `_monitoring_service()` factory 调用时构建（无 import-time 背书
/ singleton / cache）。

⚠️ Runtime backing（`_WS_POSITIONS` / `_WS_LOCK` / `_RECENTLY_GHOSTED` /
worker flag / heartbeat ts dict 等） = **identity 保留**（bundle fresh，
backing 不"new"）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class MonitoringRuntimeDeps:
    """Clock/log/summary/heartbeat + misc runtime utility（无 backing）。"""
    log: object
    now: object
    summ: object          # 持仓 summary（monitor_all 有 close 才触发）
    ghb: object           # heartbeat getter（共享 PM 模块全局）
    shb: object           # heartbeat setter（单 backing 保留）


@dataclass(frozen=True)
class MonitoringStateDeps:
    load: object
    save: object          # PMB-19 双 save：两个独立 call（不允许合并）
    wcr: object           # was_closed_recently
    mc: object            # mark_closed
    gq: object            # ghost queue backing（identity shared）
    trgt: object          # try_record_ghost_trade


@dataclass(frozen=True)
class MonitoringMarketDeps:
    s6: object
    dc: object            # data cache helper
    cfg: object
    fund: object          # P8-03 funding helper（identity 保留）
    us: object
    pc: object
    rq: object


@dataclass(frozen=True)
class MonitoringActionDeps:
    cls: object           # close_fn（决策触发的平仓）
    gcl: object           # ghost cleanup Step 0
    elm: object
    stag: object
    cts: object
    pt: object
    pp: object
    g1h: object


@dataclass(frozen=True)
class MonitoringWsDeps:
    wsp: object           # _WS_POSITIONS（identity shared）
    wsl: object           # _WS_LOCK（identity shared）
    swlu: object          # set ws last update
    wst: object
    m1: object
    lkey: object
    lttl: object
    inst: object
    ldr: object
    lkfn: object
    wsf: object
    oofn: object
    oe: object
    oc: object
