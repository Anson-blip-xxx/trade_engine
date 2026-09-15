# Phase 8-05C — Constructor Grouping Integration Closure

> @ C3 收口。PRODUCTION 0 CHANGE；tests + docs only。
> 命题验证：**40→5 / 22→5 / 21→5** 三次结构变化后，factory-time late binding、
> monkeypatch seam、runtime identity、交易行为仍与 Phase 7 closure 时一致。

## 一、Final Constructor Matrix

| Service | Before | After | Decision |
|---|---|---|---|
| ExecutionService | 1 | 1 | **NO CHANGE**（低收益） |
| ProtectionService | 5 | 5 | **NO CHANGE** |
| PositionStateService | 5 | 5 | **NO CHANGE** |
| LifecycleService | 21 loose | **5 bundles** | P8-05B1 `20b7dbb` |
| ReconcileService | 22 loose | **5 bundles** | P8-05B2 `cfd9824` |
| MonitoringService | 40 loose | **5 bundles** | P8-05B3 `59c5064` |

不是"六个 Service 全部统一"——**deliberately 保不动**三个轻量域。

## 二、Final Bundle Matrix

| Service | Bundle | 字段 | mutable? | identity-sensitive? | monkeypatch-sensitive? |
|---|---|---|---|---|---|
| Lifecycle | Runtime | log, now | ✗ | — | — |
| | Execution | exec_fn, ci, pi | ✗ | ✗ | ✓（execution seam） |
| | State | load, save, wcr, mc, clr, posid, rq | ✗ | ✓ | ✓ |
| | Protection | wkr, enq, acx, cxa | ✗ | ✓ | ✓ |
| | Action | close_fn, s6, sandbox, pg, lce | ✗ | ✓ | ✓ |
| Reconcile | Runtime | lgt, now | ✗ | — | — |
| | State | load, save, wcr, mc, posid, rq | ✗ | ✓ | ✓ |
| | Coordination | lacq, lrel, rdget, rdset, pid, uid | ✗ | ✓ | ✓ |
| | Notification | pg, tgt, tgc, rqst | ✗ | ✓ | ✓（PMB-26 吞错不对称） |
| | Action | exf, gpx, s6, sandbox, sk | ✗ | ✓ | ✓ |
| Monitoring | Runtime | log, now, summ, ghb, shb | ghb/shb via PM backing | ✓ | ✓ |
| | State | load, save, wcr, mc, gq, trgt | gq backing shared | ✓（ghost queue） | ✓ |
| | Market | s6, dc, cfg, fund, us, pc, rq | ✗ | ✓（funding seam） | ✓ |
| | Action | cls, gcl, elm, stag, cts, pt, pp, g1h | ✗ | ✓ | ✓ |
| | WS | wsp, wsl, swlu, wst, m1, lkey, lttl, inst, ldr, lkfn, wsf, oofn, oe, oc | wsp/wsl = runtime backing | ✓✓（WS identity） | ✓（callback/leader） |

## 三、Late-binding Closure 用例股份（66.7% 分数一一统测）

```text
三 service × patch PM seam → factory#1 → patch → factory#2
→ svc1 旧 implements capture；svc2 新 gets patch
（bundle 不 dynamic 反查 PM）
```

## 四、Runtime Identity Closure

- `_WS_POSITIONS`/`_WS_LOCK`/`_RECENTLY_GHOSTED`/heartbeat dict /
  worker flag —— **bundle fresh ≠ backing new**（identity 保观）
- Reconcile 不持有 ghost queue；Lifecycle 不持有 monitoring runtime
- 单 backing —— PM 唯一 owner

## 五、No Import-time Capture / No DI Framework / No Dependency Direction Reverse

```
- 全 6 service 源码与 deps.py：无 DEFAULT_*_DEPS / cached factory 结果 /
  __getattr__ dynamic lookup / Container / Injector / ServiceLocator
- pm factory —→ bundles —→ service 单向，无 reverse import
```

## 六、Compatibility Properties Audit

Monitoring（~40 observables）/Reconcile/Lifecycle 三者保留 read-only
properties；均返回 **bundle 中 captured seam**（非 dynamic 反查 PM 家庭），
与 patch-between semantics 兼容。

## 七、Body-diff Audit — P8-05B1/B2/B3 全部

- **instrumentation 仅属于 `self.foo → self.domain.foo`**
- branch reorder / try-except 调整 / helper extraction / call-count change /
  condition change / return change → 0
- Call-count：monitor_one 内 fund 调用 n==1（未 prefetch/memoize）
- Per-symbol isolation：monitor_all 循环粒度不变
- PMB-17/18/19/22/23/24/25/26/27/28/29/30 —— 全部 frozen

## 八、Constructor Direct Caller Audit

Production 侧仅 PM factory（`_lifecycle_service()/_reconcile_service()/
_monitoring_service()`）调用 services 构造；无其他 prod caller；
commit cfd9824/59c5064 后仅 PM wrapper 走 bundle 化 ctor。

## 九、C3 Inventory — **PASS / CLOSED**

C3 Large Constructor Dependency Grouping（Inventory #C3）：
- Lifecycle/Reconcile/Monitoring → 5-bundle each
- Execution/State/Protection deliberately unchanged

## 十、C9 Inventory — **仍然 DEFERRED**

**收紧**：Dependency Bundle Dataclass != Business Request/Context Dataclass。
C9 原目标是 OpenRequest / CloseRequest / MonitorContext 等 **行为侧**
对象——Bundle grouping 不等于 C9。**C9 保持 DEFERRED**（future Phase 8+
ticket；若进入需显式 P8-07+ 行为票）。

## 十一、P8-05C Exit：20 criteria 全绿（tests 22+ 新增，见 §22）
