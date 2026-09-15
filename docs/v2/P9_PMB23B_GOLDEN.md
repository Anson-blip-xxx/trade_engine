# Phase 9-05A — PMB-23B Ghost Cleanup Batch Isolation（docs/tests only；production 0 diff）

## 一、Call Graph

```
ghost_cleanup(positions, system_filter)
  ⇓ sandbox → []
  ⇓ try: record_trade = s6()[7] except → noop
  ⇓ real_r = exf('/fapi/v2/positionRisk')
     非 list → return []（宁漏不错）
  ⇓ for sym in list(positions.keys()):
       sym in real_syms → continue （交易所仍在仓 → skip）
       pos 缺 → continue
       wcr(sym) → pop + continue (recently closed dedup)
       system_filter 不匹配 → continue
       lacq(pm:ghost_close:{sym}, ttl60) → false → continue（无锁不清理）
       try:
           ghost_cleanup_one(sym, pos, positions, record_trade, closed)
       finally:
           lrel(lock_key)
  ⇓ closed非空 → log『[幽灵清理完毕]』
  ⇔ except (outer) → log『[幽灵检测异常]』→ return closed-so-far
```

## 二、Try-boundary map

- **outer try**（fully wraps batch）→ `except Exception as e` swallow → **中断后续**
  **batch-wide**
- **inner try/finally**（只包 ghost_cleanup_one（ibiliut cleanup_one），finally 释放
  lock record 语义）
- 无 per-symbol isolation——one symbol inner raise 后后续全部 skip

## 三、Core Bug Reproduction

`test_b_failure_stops_c_current`：A 成功 → B cleanup_one 内 raise →
C 未处理、positions 仍含 CUSDT → 外层 catch 打 log→ return，符号本轮丢失。

`test_marker_failure_interrupts_batch` → 复现 infra-level marker raise
同样走 outer catch（interrupts C）。

## 四、Failure-Injection Matrix（frozen）

| 失败点 | Current effect | batch continues? |
|---|---|---|
| record raise | outer catch | ✗（后续 symbol all skip）|
| marker/state.mc raise | outer catch | ✗ |
| pop/save raise | outer catch | ✗ |
| lock-acquire false | continue（无有损） | ✓ |
| lock release raise（finally） | outer catch | ✗ |
| 系统无锁 symmetry | continue | ✓ |

## 五、State/queue effects

- failed symbol：pop 已发生（PMB-23A 分票），但 **batch break 之后**的
  symbols **本轮丢失**；
- Restored via next `_load`/reconcile（PMB-23A 相关，不是 B 修复范围）。

## 六、Lock Semantics（frozen）
- key `pm:ghost_close:{sym}`，TTL 60，owner `ghost-cleanup:{pid}:{hex8}`
- acquire fail → skip（per symbol，continue），不 pop 不 record
- lock 内：pop-first → record → mark；lock-held final distribution OK
- release always in finally（inner 无 exception suppress）

## 七、PMB-23A separation

A = pop-before-record（内部单 symbol 内的 mutation 窗口）
B = outer-try granularity（batch-level）
两者都是 PMB-23 拆分子项；本票只修 B，且 A 语义完全独立。

## 八、Monitoring interaction（frozen）

- 仅 `monitor_all` 调 `ghost_cleanup`（Step 0）
- 若 B raises → outer catch 出现 → `closed-so-far` 已经保存 A 的 state
  → summary 打 log；其它 symbol 下一轮循环才会 catch up

## 九、Candidate fix（P9-05B scoping）

```
for sym in list(positions.keys()):
    if sym in …（同前 guard）
    try:
        ...
    except Exception as e:
        log(f'[ghost cleanup 异常] {sym}: {e}')
        continue
```
- per-symbol 内 lock/pop/record/mark/release **顺序不变**
- outer try 仍存在（s6api/exf fetch层）——候选：把 per-symbol 迭代从 outer
  try 中拆出（场景与 P9-03B 一致——“先证明失败再修”）
- **不得引入** broad Exception outside current type（`Exception` 保持）

## 十、P9-05B Intended Delta

```python
# 把 per-symbol isolation 下沉到 loop 层（不改变 lock/pop/record/mark/release 序）
for sym in list(positions.keys()):
    try:
        _precheck_sym(sym, positions)
        ...
    except Exception as e:
        self.runtime.lgt(f'[幽灵清理异常] {sym}: {e}')
        continue
```
Outer try 仅保留 _s6api/record_trade fetch/exf 全局问题；
inner cleanup_one 内部顺序/锁 / record-inside-lock **不动**。

## 十一、Rollback

单 commit revert。
