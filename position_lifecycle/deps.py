"""LifecycleService dependency bundles（P8-05B1；data-only holder）。

leaf module（禁止 import PM/services）；
bundle 实例由 pm `_lifecycle_service()` factory 调用时构建（无 import-time 背书）。
frozen 仅冻结 attributes reassignment——underlying runtime mutable backing
不复制（identity 按 head seam）。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LifecycleRuntimeDeps:
    log: object
    now: object


@dataclass(frozen=True)
class LifecycleExecutionDeps:
    exec_fn: object
    ci: object
    pi: object


@dataclass(frozen=True)
class LifecycleStateDeps:
    load: object
    save: object
    wcr: object
    mc: object
    clr: object
    posid: object
    rq: object


@dataclass(frozen=True)
class LifecycleProtectionDeps:
    wkr: object
    enq: object
    acx: object
    cxa: object


@dataclass(frozen=True)
class LifecycleActionDeps:
    close_fn: object
    s6: object
    sandbox: object
    pg: object
    lce: object
