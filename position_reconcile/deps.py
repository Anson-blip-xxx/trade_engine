"""ReconcileService dependency bundles（P8-05B2；data-only holder）。

leaf module（禁 import PM/monitor/ledger/state concrete）；
bundle 实例由 pm `_reconcile_service()` factory 调用时构建（无 import-time
背书/singleton/cached）。frozen 仅冻结 attributes reassignment——underlying
runtime backing 不复制（identity 按 head seam）。

⚠️ PMB-26 冻结：notification 字段独立持有（tg 吞错 / pg 传播 / alert
30s-24h 非原子 dedup），禁止任何统一 persist_and_notify()。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ReconcileRuntimeDeps:
    lgt: object      # log
    now: object


@dataclass(frozen=True)
class ReconcileStateDeps:
    load: object
    save: object
    wcr: object      # was_closed_recently
    mc: object       # mark_closed
    posid: object
    rq: object


@dataclass(frozen=True)
class ReconcileCoordinationDeps:
    lacq: object     # lock acquire
    lrel: object     # lock release
    rdget: object    # alert pending/seen redis get
    rdset: object    # alert pending/seen redis set
    pid: object      # pid source
    uid: object      # uid source


@dataclass(frozen=True)
class ReconcileNotificationDeps:
    pg: object       # PG record（失败传播 == PMB-26 frozen）
    tgt: object      # TG token
    tgc: object      # TG chat
    rqst: object     # legacy transport holder（shared seam })


@dataclass(frozen=True)
class ReconcileActionDeps:
    exf: object      # exchange positions fetch
    gpx: object      # price helper
    s6: object
    sandbox: object
    sk: object       # SYSTEM_KEYS（migration 域）
