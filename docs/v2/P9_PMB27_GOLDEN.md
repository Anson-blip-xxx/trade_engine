# Phase 9-04A — PMB-27 Partial Marker / Recent-Guard Characterization
# （docs+tests only；production 0 diff）

## 一、Marker Call Graph（真实 HEAD）

| Path | Before（guard） | During | After（marker） |
|---|---|---|---|
| full close 成功 | `_was_closed_recently`（非 force 才 check） | mark_closed（执行前） | keep |
| full close（exchange fail/exception） | 同上 | mark（前） | **clear**（失败时） |
| partial fill close | 同上 | mark（前） | **keep**（PMB-27 根因） |
| no fill（executedQty<0.001） | 同上 | mark | **keep** |
| exchange-flat close | 同上 | mark | keep |
| monitor close（_close 非 force） | check（skip if recent） | mark | keep/clear |
| manual close（force=True） | 跳过 guard | mark | — |
| partial close（_partial_close） | 无 check（独立 branch） | **从不 mark** | — |

**Marker source（确认）**：`_close` full-attempt 内 partial/no-fill outcome 残留
marker；`_partial_close`（策略层独立部分平仓）不经 `_close`，从不涉及 marker。
PMB-27 真实源头 = `_close` 内部 partial/no-fill outcome。

## 二、Partial Close Sequence（frozen）

mark → risk read#1 → round → close intent → exec → risk read#2 remaining →
`pos.qty=remaining` → `_save`（先于 REC/PG，PMB-28）→ record(final=False) →
PG CLOSE_ORDER_PARTIAL → return False；**无 cancel_all_algo**；**marker 保留**。

## 三、Recent Guard 位置（frozen）

- `_close` 入口 `if not force and wcr(symbol): return False`（在 exchange read 前）
- `force=True` bypass（manual close path）
- 第二次 close（non-force）→ marker guard 在 risk read 之前拦截

## 四、核心 Reproduction（deterministic）

1. local qty=10；close → partial fill → local qty=6
2. marker：keep（PMB-27 persist）
3. 第二次 non-force close → **flag recent guard → False**（BUG）
4. force=True 第二次不拦（manual path 通）

## 五、Marker Lifecycle Matrix

见 test：full-success-keep / failure-clear / partial-keep / no-fill-keep /
flat-keep / exception-clear —— 与 P9-04A frozen 矩阵一致。

## 六、Partial fill × second-close 当前行为

non-force second close → **被拦**（monitor close 二轮错过）；
force=True（external 手动）不拦。

## 七、State Persistence（frozen）

partial 后 remaining qty/entry/meta/position_id/algo_sl_id 全保留（P7-03A frozen）。
无 marker fix state-shape 意外改动。

## 八、Return contract（frozen）

`_close` partial/no-fill：**False**；`_partial_close`：None；`monitor_all` 5-tuple
监控逻辑不变。

## 九、Marker Scope / cross-position risk

- marker key = **symbol**（非 position_id/side）—— 4h window
- 同 symbol 重开（4h 内）→ strategy 层先挡（has_any_position / has_position）；
  cross-position 不动（非 PMB-27 修复范围）

## 十、T12 / T1 分离

- T12（save-order symmetry）和 T1（duplicate open，已 CLOSED）与 PMB-27 独立
- 本票只修 marker lifecycle 全链，不动 save-order/execution return/ledger

## 十一、候选方案比较

| 方案 | 行动 | 风险 | 复杂度 | 回滚 |
|---|---|---|---|---|
| **A. clear marker on partial/no-fill** | partial / no-fill close-outcome 尾部 clear | partial→full race 局部窗口 | low | single revert |
| B. only confirmed-full close 才 mark | marker 放到 exec 成功后 | marker-first 语义反转 PMB-4 多进程 | HIGH | high |
| C. guard 增加 remaining-position exemption | guard 时 local qty>0 可过 | 复杂+跨模块 | HIGH | high |

**Recommendation：A（minimal）** —— `_close` partial / no-fill return False 前
补一行 `self.state.clr(symbol)`；其它 path 不动。

## 十二、Recommended P9-04B Delta（计划，未 implement）

```python
# `_close` 内 partial-fill / no-fill 分支 return False 之前：
self.state.clr(symbol)      # P9-04B fix（PMB-27）—— partial 不留 recent marker
```
rollback single revert。


---

# FIXED（P9-04B）

- **FIXED BY:** `fix(v2): clear recent marker after non-final close`（commit pending）
- **OLD**: partial-fill / no-fill → marker keep → 后续 non-force close 被 recent guard 拦
- **NEW**: partial/no-fill return False 前 `self.state.clr(symbol)` —— marker clear
- Full success / Execution failure / Exchange-flat / exception paths：unchanged
- Scope：non-final close outcome only（partial / no-fill）
- 其它：force semantics unchanged；recent guard/4h/symbol key unchanged；
  `_partial_close` unchanged；T12 untouched
- **Rollback**: `git revert <P9-04B>"`（P9-04A characterization `994a8ca` 保留）
