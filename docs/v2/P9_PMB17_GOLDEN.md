# Phase 9-06A — PMB-17 Malformed Ghost Queue Tuple（docs/tests only；Production 0 diff）

## 一、Queue Schema（HEAD）

`ghost queue item`（wellformed shape）—— **6-tuple**：
`(sym, reason, price, entry, qty, side)`（index 5 = `side`）
consumer parser：`g_side = g[5] if len(g) >= 6 else None`

## 二、Producer Inventory

全仓库搜索：`_RECENTLY_GHOSTED.append` **0 调用点**（PMB-25 dead runtime）。
malformed item 只会来自历史残留 / 外部写入；consumer 支持保留。

## 三、Consumer parser（frozen 逐字）

```
while gq:
    g = gq.pop(0)
    g_sym = g[0]                                   # IndexError 若 empty tuple
    g_side = g[5] if len(g) >= 6 else None         # len<6 → side=None
    if g_sym in closed_syms:                        continue
    if not system_filter or not g_side:             # ← BYPASS root
        closed.append(g); closed_syms.add(g_sym)
    elif system_filter == 'S6' and g_side == 'LONG': …
    elif system_filter == 'S8' and g_side == 'SHORT': …
    else:
        remaining.append(g)
gq.extend(remaining)
```

## 四、side=None Bypass root cause（frozen）

`not g_side`（presence != validity）：None / '' / 未知 / 5-tuple / str-item 都
被当作 allowed-branch 进入 →是该 entry per-symbol 亦以 bump filter 语义。

## 五、Malformed Matrix

| Item | Parser | Result | Batch |
|---|---|---|---|
| 5-tuple（无 side） | side=None | **consume（bypass）** | done，无 reload |
| empty tuple | `g[0]` IndexError | **直接 raise**（monitor svc层不吞） | raised |
| str `'X'`（len1） | side=None | **consume** | done |
| side='' | `not g_side` → consume | consume | done |
| side='UNKNOWN' | unmatched → `remaining` requeue | requeue ✓ | 保留 |
| extra fields (>6) | `g[5]` index 5 取 side 继续合理 | consume | done |

## 六、core reproduction（deterministic）

`test_short_tuple_side_none_bypassed_consumed`：`('B','x',5,5,10)` with `S8 filter`
→ **consume**（原 expected 应为 retry / 无 consume）；**bypass frozen**。

## 七、Consumption Semantics（frozen）

- **先 pop 再处理**（process_后 pop）—— entry 一经 parse lost
- invalid malformed items：**permanent drop（lost）**—— no requeue/retry
- side='UNKNOWN' 进入 requeue（ EXCEPTION path is preserved）

## 八、Downstream Effects

- B was consumed → not mark / state mutation / cleanup —— **not touched**
  (只是 closed_syms 加 elem，后期 summary 中可能 unseen)
- `monitor summary` count 影响 | summary visible in monitor_all 步骤

## 九、Monitoring Step Order（frozen）

monitor_all Step0 = ghost_cleanup → queue consumption 在 **Step no=loop
final phase**（consumption zone）；其本身**无 catch**（演示
`test_empty_tuple_indexerror_from_monitor_level` —— Index error 直接 raise）。

## 十、Risk Classification

- **Confirmed**：5-tuple / side=None / str-item / empty tuple —— bypass 消费
  or raise；downstream 可产生错误记账/告警
- **Plausible**：跨进程错误方向处理——**plausible**（需要 producer 死当前的
  historical факtor；无 active producer → 风险实际边界 limited）

## 十一、候选修复 A/B/C/D

| 方案 | 行动 | 数据丢失 | 兼容 | 复杂度 | 危 |
|---|---|---|---|---|---|
| **A. malformed → log + drop** | len<6 → continue（after log） | low | ✓ | low | low |
| **B. malformed → log + requeue** | requeue attempt | retry risk（可能 loop）| ✓ | medium | medium |
| C. derive side from local state | 要读 state | ✓ | ✓ | medium | medium |
| D. producer-side schema validation + reject | 加 schema | n/a | ✓ | high | medium |

**Recommendation P9-06B：方案 A（minimal）**（consumer-side len验证 + log-drop）
—— zero risk，无 retry/requeue，producer dead（向后兼容：仅 static。

## 十二、PMB-23 对比：非同一根

- PMB-23A/B 独立（人 split pass）—— PMB-17 queue schema / malformed item 是
  consumer parser层， unrelated cleanup helpers。
