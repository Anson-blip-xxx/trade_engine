# PM Reconcile / Ghost Golden Index — P7-06A

> 冻结 `reconcile_all` / `_ghost_cleanup` / `_try_record_ghost_trade` /
> external-position alert / ghost 队列的当前行为，作为 P7-06B（Ghost /
> Reconcile Service 抽取）的迁移约束。**生产代码零改动。**

## Call Graph（当前 HEAD 真实提取）

```
monitor_all (MonitoringService.monitor_all)
  └─ Step 0: _ghost_cleanup(positions, system_filter)      ← filter 之前
       ├─ _sandbox_active() → True → return []
       ├─ _s6api()[7] → record_trade（失败 → noop lambda）
       ├─ _light_fapi_get('/fapi/v2/positionRisk')
       │    ├─ 非列表 → return []（宁漏不错）
       │    └─ dict & |positionAmt|>=0.001 → real_syms
       ├─ loop list(positions.keys()):                      ← 外层合成 try 包全环
       │    sym in real_syms → continue
       │    pos 缺 → continue
       │    _was_closed_recently(sym) → pop + continue      ← 无记账
       │    system_filter 不匹配 → continue                 ← 不 pop（保对方仓）
       │    _lock_acquire('pm:ghost_close:{sym}', owner, ttl=60)
       │      fail → continue（不 pop）
       │      ├─ _ghost_cleanup_one（lock 内）:
       │      │    positions.pop(sym)                        ← record 之前！
       │      │    ghost_price = _light_get_price(sym) or entry
       │      │    record_trade(... exit_reason='手动平仓',
       │      │                  final_close=True, ghost_cleanup=True)
       │      │    _mark_closed(sym)
       │      │    closed.append(sym,'手动平仓',ghost_price,entry,qty,side)
       │      └─ finally _lock_release
       └─ closed 非空 → '[幽灵清理完毕]'
（异常）→ 外层 except '[幽灵检测异常]' → 整个 loop 中止（PMB-23）

monitor_all 终段：
  └─ 消费 _RECENTLY_GHOSTED（无生产 producer —— dead queue，PMB-25）

reconcile_all（独立入口，不由 monitor_all 触发）
  ├─ fapi_get('/fapi/v2/positionRisk')
  │    ├─ 非列表 → '[对账跳过]' return [],[]
  │    └─ 异常 → '[对账失败]' return [],[]
  ├─ 实仓解析：|positionAmt|>=0.001（无 tradable 过滤——PMB-24）
  ├─ Local-only → pop + '[对账] 幽灵仓清除' + ghost.append   ← 无 record/无 lock/无 mark
  ├─ Exchange-only → '[对账] 漏记仓' + missing.append        ← 无 adoption
  └─ _save(positions)（一次）

merge 链（_load REST/WS 层）：
  _merge_meta(alert_external=True)
    ├─ 非 tradable symbol → skip
    ├─ was_closed_recently → _clear_closed_marker + '[闭标记修复]'
    ├─ meta 无 → _notify_external_position（per-side S6/S8 归因）
    ├─ meta 有 → 清 alert pending key
    └─ _merge_meta_preserving_missing：local-only 未 closed →
        保回 merged（可以 creaded ghost 流程核验）

WS 侧 record（P7-05B 已冻结）：
  ACCOUNT_UPDATE |pa|<0.001 → pop → _try_record_ghost_trade：
    lock('pm:ghost_close:{sym}', return False if busy)
    ├─ lock 内：was_closed_recently → False（已 _close 记账过）
    ├─ record_trade(exit_reason='幽灵仓关闭', final_close=True,
    │              ghost_cleanup=True)
    ├─ 异常 → '[幽灵记录失败]' False
    └─ finally release
  成功后调用方 _mark_closed（record→mark 顺序，PMB-21）
```

## 测试文件 → 冻结行为

| 文件 | 测试数 | 冻结范围 |
|---|---|---|
| `test_ghost_local_only_golden.py` | 17 | 交易所过滤/微量清理 / recently-closed / record→mark 序 / lock key / PMB-23 |
| `test_ghost_lock_golden.py` | 8 | ghost 记账锁契约 / lock 内 recently-check / payload shape |
| `test_reconcile_input_golden.py` | 12 | reconcile 输入 + 两类 mismatch + 无 adoption + migrate |
| `test_ghost_queue_golden.py` | 7 | FIFO / malformed / duplicate / PMB-17 / restart |
| `test_external_position_golden.py` | 15 | pending→grace 30s→seen 24h / system 归因 / TG 吞错 / PG 不吞 |
| `test_reconcile_dedup_golden.py` | 4 | marker 自愈 / meta_filtered 丢弃 / WS→reconcile 无双记 |

## 两层 mismatch 分开冻结（关键差异）

| 通道 | Local-only（ghost_cleanup） | Local-only（reconcile_all） | Exchange-only |
|---|---|---|---|
| record_trade | ✓（lock 内） | ✗ | ✗ |
| _mark_closed | ✓ | ✗ | ✗ |
| lock | ✓ | ✗ | — |
| pop local | ✓ | ✓ | 不写 state |
| notify | ✗ | ✗ | `_notify_external_position`（merge 链） |
| adoption | ✗ | ✗ | ✗（只 missing 列表；启动时
  `migrate_existing_positions` 从 `state:s6/s8` 迁入） |

## 失败拓扑速查

| 失败点 | 本轮/后续 symbol | state | record | lock | notify |
|---|---|---|---|---|---|
| exchange API（非列表） | 不动任何仓 | 未改 | ✗ | ✗ | ✗ |
| record raise（ghost） | 本轮 pop 已发生、后继仓停止 | **本地已丢** | 局部 | released | ✗ |
| lock fail | 该仓 skip（不 pop） | 未改 | ✗ | ✗ | ✗ |
| TG 失败（external） | 不影响后续 | unchanged | ✗ | ✗ | ✗（ swallowed） |
| PG record 失败（external） | **抛出调用方** | unchanged | ✗ | ✗ | 中止 |
| malformed queue item | 消费继续（后续完） | 未改 | ✗ | ✗ | ✗ |

## 索引既有观察

- PMB-17（ghost 队列 side 缺失 → filter 失效消费）—— `test_short_tuple_consumed_filter_bypass`
- PMB-21（WS record→mark 顺序）—— `Card 21@PM_MONITOR_GOLDEN_INDEX`
- S0-9 家族（closed marker 无 TTL / ts 窗口）—— `PM_STATE_GOLDEN_INDEX`
- PMB-4（marker 实际永远存在直到 clear）—— `PM_GOLDEN_OBSERVATIONS`
- OBS-3 / PMB-2（_POS_CACHE 双写）—— 不涉及本链运行时

## 新观察（PMB-23 ~ PMB-26，详见 PM_GOLDEN_OBSERVATIONS.md）

- **PMB-23** cleanup_one `pop 先于 record`；record 失败 → 本地丢失且无记账；
  且 `_ghost_cleanup` 外层 try 包全环 —— cleanup_one raise 中止后继仓
- **PMB-24** reconcile 幽灵清理静默（无 record/mark/lock/tradable filter）
- **PMB-25** `_RECENTLY_GHOSTED` 无生产 producer（dead queue，仅消费）
- **PMB-26** merge 链闭标记自愈（ обмена alive → marker 清除）+ TG/PG 吞错不对称

## P7-06B ReconcileService 边界建议（设计，未实现）

```text
PositionReconcileService（独立于 MonitoringService，不合并）
  职责: local/exchange 对账、ghost 记账执行、external-position alert、
        recently-ghosted runtime、migrate
  注入: state load/save, ledger record, protection cancel, notify(TG/PG),
        lock acquire/release, clock, exchange positions fn, close_fn
  不拥有: _close lifecycle 实现（P7-07）
MonitoringService 保持：gcl / 一旦 P7-06B 落地 → reconcile_fn / ghost_cleanup_fn
```

---

# P7-06B · Reconcile Service Extraction Closure

> commit：`refactor(v2): extract PositionManager reconcile service`

## Ownership 迁移

| 职责 | Before | After |
|---|---|---|
| `_ghost_cleanup`/`_ghost_cleanup_one`（通道 A：lock→record→mark→pop） | PM inline | `position_reconcile/service.py`（逐字；PMB-23 序不变） |
| `reconcile_all`（通道 B：silent pop/list，无 record/mark/lock） | PM inline | service（逐字；PMB-24 不变） |
| `_try_record_ghost_trade`（WS record 通道；lock 内二次去重） | PM inline | service（逐字） |
| `_notify_external_position`（30s pending / 24h seen / TG 吞 / PG 传） | PM inline | service（逐字；PMB-26 不变） |
| `migrate_existing_positions`（state:s6/s8 启动迁移） | PM inline | service（逐字；无 runtime adoption） |
| ghost queue `_RECENTLY_GHOSTED` 消费 | monitoring（P7-05B） | **不动**（dead runtime 仍 monitoring 侧） |
| `_close` lifecycle | PM | **不迁（P7-07）** |

## ReconcileService API（注入契约，晚绑定 via `pm._reconcile_service()`）

`PositionReconcileService(lgt, now, load, save, sandbox, exf, gpx, lacq, lrel, wcr, mc, s6, posid, rdget, rdset, rqst, tgt, tgc, pg, sk, pid, uid)`

依赖方向：零反向 import（AST seal：禁 PM/shared_executor/monitoring/state/ledger/protection/execution）。

## Legacy wrappers（PM 保留 thin delegate）

`_ghost_cleanup` / `_ghost_cleanup_one` / `_try_record_ghost_trade` /
`_notify_external_position` / `reconcile_all` / `migrate_existing_positions`

## 新增测试

- `test_reconcile_service_architecture.py`（9）：AST seal / clean import / 6 wrapper delegate parity
- `test_reconcile_service_parity.py`（11）：通道 A/B/WS/migrate 直接构造 parity；
  PMB-23 pop-before-record；lock fail/release；grace→seen 告警；双路径一致
- `tests/execution/test_protection_boundary.py` grace 常量 guard 目标随 ownership
  迁移（常量本身 30s 未变——architecture guard adjustment）
