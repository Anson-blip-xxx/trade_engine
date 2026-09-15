# Phase 9-07A — PMB-26A Closed-Marker Self-Heal（docs/tests only；production 0 diff）

## 一、Self-Heal Call Graph（真实 HEAD）

```
_load / merge 链
  └─ _merge_meta(raw, meta, now, alert_external=True)
       └─ for sym, bp in raw.items():
            else → [闭标记修复] 实际仍有持仓，清除关闭标记
                    └─ _clear_closed_marker(sym)
            （marker self-heal：exchange reality wins over stale marker）
            mp = meta.pop(sym)（原地 mutation，PMB-28 owner）
            not mp & alert_external → _notify_external_position（per-side S6/S8）
            elif mp → rset pending key {}（清 stale pending）
```

## 二、Trigger Matrix

| 条件 | Self-heal 触发？ |
|---|---|
| exchange has position (raw 含 sym) + fresh closed marker | **YES（heal）** |
| exchange has position (raw 含 sym) + **expired** marker | NO（marker 已过期，`wcr=False`，不进 heal 分支——
  stale marker 永久遗留是 PMB-4 deferred 票，本票不动） |
| exchange has position (raw 含 sym) + **local state exists** | NO（无 heal，正 sufficiency） |
| marker exists (wcr=True) + **exchange missing**（raw 空） | NO（loop 不含该 sym → marker 保留，
  ghost/reconcile other flow） |
| system_filter 不匹配 | 不影响 heal（heal 与 filter 无关，merge 链独立） |
| malformed marker / invalid ts | `wcr=False` → 不触发 heal；同 PMB-4 遗留 |

**Authority model（frozen）**：
- exchange reality（positionRisk/交易所实仓）> local closed marker
- marker 与 exchange 冲突 → **清 marker** + 继续正常流程

## 二、Core Reproduction（deterministic）

`test_fresh_marker_plus_exchange_position_clears`：merge chain + marker
为 fresh（`wcr=True` patched）→ `_clear_closed_marker(sym)` 调用一次；
heal 后 notify 顺序 -> notify external 因为无 local meta。

## 三、local / exchange 矩阵

| local | exchange | heal? | state | notify | save |
|---|---|---|---|---|---|
| exists | exists | NO | enrich via meta | NO (mp exists) | NO |
| missing | exists | **YES** | rejoin merged dict | YES | NO（no-save in heal） |
| exists | missing | — | pop via band | — | — |
| missing | missing | — | — | — | — |

## 四、Marker Age Matrix

| Age | was_closed_recently | Heal? |
|---|---|---|
| fresh (<4h) | True | **YES** |
| expired (>4h) | False | NO（stale marker stays；PMB-4）|
| invalid ts | False | — |

## 五、Notification Ordering（frozen）

- marker clear **先** notify（merge 链 start 为 marker check first；
  后 iterate meta pop/notify）
- heal 本身不通知；heal 后因为 mp empty → notify external

## 六、Pending / Seen Interaction

- heal path with no local meta → notify external → pending key 写入
  fingerprint + ts（两段 dedup 首观察）
- seen key 仍 absent / 24h window 不动

## 七、Return contract

`_merge_meta` → `merged dict`（enriched/tradable only）。self-heal 不改 return type。

## 八、Risk Assessment / Conclusion

**PMB-26A conclusion = KEEP AS-IS（INTENTIONAL SELF-HEAL）**
- exchange reality wins → 自动恢复 stale/mismatch 的 closed marker，
  去除 dedup 窗口对真实仓位的长期阻断
- **未涉及** TG/PG（PMB-26B/C 分票）; `notify` exception 拓扑 frozen
  （本阶段 0 diff）
- PMB-27（close lifecycle）和 PMB-24（silent reconcile）本票分离

→ **PMB-26A → REVIEWED / INTENTIONAL / CLOSED.** 无 production ticket。
