# Protection / Monitoring / Side-Effect Boundary Inventory（Phase 4-03-01-D4）

> 基于 feature/v2-architecture @ `1636a6e` 实际代码逐行核实。
> 目标：盘点 PM 的保护/监控/副作用职责并建立最小 boundary（NO-WIRING）。
> **零行为变化**：不修 11s 窗口、不改线程模型、不统一 TG、不修 ghost。

---

## A. Protection Order（Algo SL/TP）

| 项 | 事实（cfac~1636a6e 行号） |
|-----|----------|
| SL price 来源 | se 路径：策略/Risk 已计算的 `stop_price` 直传 `open_position`；PM be_done：`round(entry*1.001/0.999)`（get_symbol_info 精度，L1353-ish）；trail：`_place_trail_sl` 计算 |
| 入队 | `PM._algo_enqueue(symbol, side, trigger, qty)`（队列元组，进程内 FIFO + `threading.Lock`） |
| worker | `_algo_start_worker`：**daemon 线程，进程级单例**（`_ALGO_WORKER_STARTED`）；se **模块导入时**即启动（L355，import 副作用）；`_algo_worker_loop`：有任务→执行→`sleep(11)`；空队列→`sleep(1)` |
| 下单 | `_algo_place_sl_inner`：raw `requests.get` exchangeInfo(公开，timeout 10) → LOT_SIZE/PRICE_FILTER 截断舍入 → **先** `_cancel_all_algo(symbol)`（防重启积累）→ `_light_fapi_post('/fapi/v1/algoOrder', {...})` → 成功把 `algo_sl_id` 写回 pm:positions（load/save），失败仅日志 |
| delay | **11s/单**（限速）；成交→挂 SL 之间为无保护窗口（E-OBS-3，已冻结不修） |
| retry | **无**——worker 消费一次即弃，失败不重新入队（仅日志） |
| exception | inner 全套 try/except → `{'error': ...}`；worker 循环再兜一层 |
| 换单 | be_done：`_algo_cancel(old_id)`（**见 PMB-9 兜底 GET 问题**）→ enqueue 新单；cancel+enqueue 顺序冻结（L1364-1366-ish） |
| trail 节流 | `<60s` 且变化 `<0.2%` → 仅本地 `pos['sl']` 更新，不发交易所（_place_trail_sl） |

## B. Monitoring

| 项 | 事实 |
|-----|------|
| 启动 | S6/S8 主循环周期性调 `se.pm_monitor(name, state, tg_fn)` |
| writer 锁 | `pm:monitor:writer` Redis lock（ttl 30，`lock_acquire` 失败→跳过本轮） |
| 主循环 | `PM.monitor_all(system_filter)`：Step0 `_ghost_cleanup` → 逐仓 `_monitor_one`（11 类退出判断：费率→硬止损→紧急→早期亏损→停滞→be_done→分层TP→trail→峰值保护→1h 反转→时间止损） |
| 数据 | `_load()` 三层：WS 实时（`_WS_POSITIONS`，**ws 后台 daemon 线程仅 leader 进程持有**）→ REST positionRisk → 本地 meta |
| 间隔 | 由调用方（策略主循环）控制；PM 内不含 polling interval（`_monitor_heartbeat_ts` 60s 心跳日志） |
| 返回 | `[(symbol, reason, close_price[, entry, qty, side]), ...]`；异常循环：多数步骤独立 try/except 吞错继续 |

## C. Ghost / Reconcile

| 不一致场景 | 处理（真实） |
|-----------|-------------|
| PM 有 / 交易所无（WS 或 REST 先确认平） | WS leader：`_try_record_ghost_trade`（锁 `pm:ghost_close:{sym}` ttl60 + `_was_closed_recently` 去重 → record_trade(ghost_cleanup=True) → **先落库后 `_mark_closed`**）；monitor_all：`_ghost_cleanup`（同锁）→ record('手动平仓') → `_mark_closed` → 移除 |
| PM 有 / 交易所无，且 4h 标记已在 | 直接 pop 不记账（`_close`/WS 已处理） |
| 交易所已有 / PM 无 | `_merge_meta(alert_external=True)` → 记入（并入系统 S6/S8 推断方向）+ `_notify_external_position` 告警（30s 去重 grace + 指纹） |
| `reconcile_all` | PM 有交易所无 → pop；交易所有 PM 无 → missing 列表（告警，不自动登记）；API 异常 → 跳过（宁漏不错） |

优先级：**Exchange 永远是真相**；本地 meta 只做 enrichment；谁删除 = ghost 流 + reconcile；
谁创建 = 只在告警后人工核（无自动建仓路径）；PG = record_trade(ghost_cleanup=True) 列；Redis = partial/`closed:` 标记。

## D. Notification

| 项 | 位置 | 语义 |
|-----|------|------|
| 平仓 TG | trade_recorder（"单一权威"通知，se.pm_monitor 注释明确不重复发） | dedup `notify:close`（se `_should_notify_close`，见 G 类） |
| 外部漏记仓告警 | `PM._notify_external_position(symbol, raw, system)` | 指纹 `side:entry:qty` → pending（30s grace）→ 每日一次 → TG `requests.post` 直连 + PG EXTERNAL_POSITION_DETECTED |
| 错误日志 | `_log_close_error`（60s 限频）、`_pmlog`（文件+stdout） | 各自吞错 |

## E. Coordination（锁/选取）

| 锁 | 参数 | 语义 |
|-----|------|------|
| `pm:monitor:writer` | ttl 30 | 双进程（S6/S8）互斥本轮监控 |
| `ws:leader` | lease 45 + renew | WS 用户数据流单连接（防止互踢） |
| `pm:ghost_close:{sym}` | ttl 60 | 幽灵记账单仓互斥 |

异常：锁服务失败 → writer 放行/WS 兜底 True（避免失联）——现状保持。

## F. Other side effects

| 项 | 事实 |
|-----|------|
| se import 副作用 | `_algo_start_worker()`（L355）— 任何 import se 的进程启动 daemon 线程（N1，P4-03-00） |
| `_ALGO_QUEUE` | 进程内存队列（非 Redis；进程重启即清空——丢失已入队未挂 SL，现状） |
| closed marker 净增 | 见 PMB-4（无 TTL） |
| `_CLOSE_ERROR_LOG_TS` / `_last_api_call`(3s) / `_last_algo_update`(60s/0.2%) | 模块级节流状态字典 |

## Algo SL 完整调用链（Open 成功后）

```text
open_position 成功（PM state→PG→TG 完成）
  → _algo_enqueue(symbol, close_side, stop_price, filled_qty)      [se L1045；side=SHORT→BUY]
      _ALGO_QUEUE.append(...)（FIFO，内存）
  ← worker daemon 线程（se import 时已启动；进程级单例）
      执行 _algo_place_sl_inner：exchangeInfo 排查/舍入
        → _cancel_all_algo(symbol)  [先清理旧条件单]
        → _light_fapi_post /fapi/v1/algoOrder（positionSide=BOTH, CONDITIONAL, STOP_MARKET,
           workingType=MARK_PRICE, GTC, reduceOnly='true'）
        → 成功：_pmlog + pm:positions 写回 algo_sl_id
        → 失败：仅日志（无 retry/requeue）
      sleep(11) → 下一个任务（空队列 sleep(1)）
```

close 侧 cancel：full 成交/平后 → `_cancel_all_algo(order 成功后)`；already-flat 分支 → record 前 cancel ✓；**rejected → 不 cancel**（避免裸仓，行为冻结）。
`_algo_place_sl_inner` 场景在沙盘：`_light_fapi_*` **无沙盘拦截**（可能真实下条件单，N3/P4-00 已记录）。

## Boundary 决定（NO-WIRING）

真实 seam 为 7 个既有函数；本阶段只契约化，**不接生产**：

```text
ProtectionPort（5 个真实 seam 逐字镜像）
  enqueue_algo_sl / start_algo_worker / place_algo_sl /
  cancel_algo_id / cancel_all_algo
NotificationPort（2 个真实 seam）
  notify_external_position / log_close_error
      ↓ adapters（callable 注入，零逻辑）
      ↓ Phase 7 wiring（当前全部直调 PM 现址）
```

NO-WIRING 理由（§10 条件）：7 个 seam 各带业务/状态（status 过滤、`algo_sl_id` 写回、
30s grace 指纹去重、60s 限频状态、线程创建）；任何一块塞入 adapter 即复制业务判断；
且调用点分散于 se/PM 编排内。故：契约 + adapter + tests + 行为冻结测试。

## Phase 7 未迁移职责

`_monitor_one`/`monitor_all` 编排、ghost/reconcile 全链、WS 线程与 leader 租约、
algo worker 线程本身、notify/节流状态、_ALGO_UPDATE 节流——全部保持 PM 现址。
