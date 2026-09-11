# Position Manager Boundary Inventory（Phase 4-03-01-D1）

> 基于 feature/v2-architecture @ `5e3bf6d` 的实际代码逐行核实。
> 目标：明确 ExecutionService 与 PositionManager 的职责边界，为 Phase 7 PM
> decomposition 建立稳定 boundary。**零行为变化**（生产代码零改动，
> contract + adapter + tests，不接入生产主链）。

---

## 1. 当前真实依赖方向（重新核实）

```text
se.open_position            PM._close / _partial_close
      │                              │
      │ (编排层)                      │ (编排层)
      ▼                              ▼
ExecutionService ──► BinancePort ──► se.fapi_post / _s6api().fapi_post
      │
      └──（PM 无关——Service 不回调 PM）
```

- **ExecutionService 不依赖 PM**：open 的 PM 注册发生在 se 编排层
  （`_update_pos_cache`），close 的结果处理发生在 PM 内部——两端都是
  "先 execute_order，后处理结果"。
- **PM → ExecutionService** 单向存在（01-C：订单提交）；**无 ExecutionService
  → PM 的反向依赖**（零循环依赖，禁止制造）。
- 结论：Boundary 的未来消费者是**编排层**（se/PM），不是 Service 本身。

## 2. PM 对外暴露能力分类（A-E）

### A. Position State

| 能力 | 实现位置 | 说明 |
|------|----------|------|
| create/open position（open 路径登记） | `shared_executor._update_pos_cache`（L366-400）⚠️ **位于 se 而非 PM 模块** | 写 `_POS_CACHE` + 直写 `pm:positions` |
| create/open position（legacy） | `PM.open_position`（L751-835，无生产调用方） | 全套 PM 内开仓（订单→SL→登记） |
| update position（be/trail/qty） | `PM._update_stop_loss` / `_place_trail_sl` / `_partial_close` / `_close` 内联 | 全部内部状态变更 |
| merge position | `PM._merge_meta` / `_merge_meta_preserving_missing`（L604-658） | exchange 快照 + 本地 meta enrich |
| close position | `PM._close`（L1698+）/ `PM.close_position`（L1658） | 主平仓入口 |
| partial close | `PM._partial_close`（L1697+） | 分层止盈 |
| get position | `PM._load`（三层：WS→REST→meta，L660+）/ se `_get_positions`/`get_position_count`/`has_any_position`（L402-446） | 防重复开仓读路径 |
| position cache | se `_POS_CACHE` / `_refresh_positions`（L357-364） | 周期内缓存 |

### B. Execution Result Handling

| 处理点 | 当前位置 | 边界状态 |
|--------|----------|----------|
| open 结果 → PM state | `se.open_position`：`parse_execution_result`（core）→ `_update_pos_cache(name, symbol, side, avg_price, filled_qty, stop_price, leverage, margin, event_type, strength)` → position_id | **唯一跨模块 seam** → 本次提取为 `PositionManagerPort.register_opened_position`（contract 冻结） |
| close 结果 → PM state | `PM._close` 内联：`has_remaining_position` / `accounted_close_qty`（core 谓词）→ `pos['qty']=remaining` 或 pop；`final_close` 标志传给 record_trade | **PM 内部，无跨模块缝**（Phase 7 才有接口化条件） |
| partial 结果 → PM state | `PM._partial_close` 内联：`remaining_after_partial`（core）→ `pos['qty']` 更新 | 同上 |

### C. Persistence

| 写入 | 实现位置 | 语义 |
|------|----------|------|
| `pm:positions`（Redis） | **双写方**：se `_update_pos_cache` 直写 `_rset`（静默吞错）与 `PM._save`（L715-720，静默吞错） | 见 PMB-1 |
| `closed:{symbol}` 标记 | `PM._mark_closed` / `_was_closed_recently` / `_clear_closed_marker`（L1670+） | 4h TTL 跨进程防重入 |
| 策略状态 `state:s6/s8` | se `load_state/save_state`（L328-347） | 非 PM 职责但共享 Redis store |
| PostgreSQL `trade_events` | `PM._close` 三分支 `_pg_record_event`（FLAT/PARTIAL/FILLED）+ `_notify_external_position` + se open 路径 | close 事件顺序 E-OBS-6a 冻结 |
| 幽灵仓记账 | `PM._try_record_ghost_trade` / `_ghost_cleanup_one`（trade_recorder） | 幽灵流 |

### D. Monitoring / Side Effects

| 能力 | 位置 | 说明 |
|------|------|------|
| TG 通知 | `PM._notify_external_position`（requests 直连） | 外部漏记仓告警 |
| Algo SL 队列/worker | `PM._algo_enqueue` / `_algo_start_worker` / `_algo_worker_loop` / `_algo_place_sl_inner` / `_algo_cancel` / `_cancel_all_algo`（L187-306） | 进程内 FIFO + 11s 线程 |
| 监控退出链 | `PM._monitor_one` / `monitor_all`（十一类退出原因编排 `_close`/`_partial_close`） | PM 核心 |
| 幽灵清理/对账/快照 | `_ghost_cleanup*` / `reconcile_all` / `log_position_summary` | — |
| 平仓错误限频日志 | `PM._log_close_error` | 60s 去重 |

### E. Exchange Interaction（Binance 直接调用点）

**已迁移到 ExecutionService（P4-03-01-B/C）**：
| 调用 | 位置 | 经由 |
|------|------|------|
| SE open MARKET order | se.open_position（经 `_execution_service()` 工厂） | ExecutionService → BinancePort → SharedExecutorBinanceAdapter(se.fapi_post) |
| PM full close MARKET order | PM._close（经 `PM._execution_service()` 工厂） | 同上（注入 `_s6api().fapi_post`，晚绑定） |
| PM partial close MARKET order | PM._partial_close（同上） | 同上 |

**仍由原模块直接处理（本阶段不迁移）**：
| 调用 | 位置 | 原因 |
|------|------|------|
| leverage / marginType POST、cancelOrder POST | se.open_position | 非"订单提交"主缝；迁移属后续 |
| positionRisk 查询（gates/has_any_position/close 确认×2） | se / PM | 查询类；未建 ReadPort |
| exchangeInfo（`_round_qty`/`_get_min_notional`） | se（core 已有纯核） | SymbolMetaPort 属 step 04 |
| Algo SL 下单/取消（`_algo_place_sl_inner`/`_cancel_all_algo`/`_algo_cancel`） | PM `_light_fapi_post/get/delete` | H 类；Phase 7 |
| s6_api 其余查询（price/symbol_info/oi/rsi/account） | `_s6api()` / `_get_balance` / `_calc_used_margin` | market data |
| **legacy `PM.open_position` 的 MARKET order（L796）** | PM | 无生产调用方（E-OBS-9 保持，不迁移） |

## 3. PositionManagerPort（本次建立的最小契约）

```text
orchestration（se/PM，未来）
    ↓ 注入
execution.ports.pm.PositionManagerPort   ←— 契约 = _update_pos_cache 签名逐字镜像
    ↓ 实现
execution.adapters.pm.PositionManagerAdapter（callable 注入，零依赖 strategies）
    ↓ wiring（Phase 7 前 = 现状直调）
shared_executor._update_pos_cache（_POS_CACHE + pm:positions）
```

- **Port 面**：单方法 `register_opened_position(name, symbol, side, entry, qty,
  stop_price, leverage, margin, event_type, strength) -> position_id`
  （parity 测试断言与真实函数签名逐字一致）
- **不暴露** Redis / PostgreSQL / Telegram / Binance / 策略判断
- **不接入生产**：Service 不消费该 port；`_update_pos_cache` 仍被 se 直调
  （有"no-wiring"状态冻结测试）。理由：把 PM 回调塞进 Service 违反
  "Service 只负责 intent→port→result"（E-OBS-1cancel 语义留在编排层）
- Adapter 无 try/except：异常/返回值语义 = 注入 callable 原样（零行为变化）

## 4. 依赖方向与循环依赖检查

- `execution/ports/pm.py`、`execution/adapters/pm.py`：仅 import `execution.core`
  / `typing`，**不 import strategies / shared** ✓（源码扫描 + 子进程 sys.modules 测试）
- `execution.service`：不引用 `PositionManagerPort`（边界未接入 Service）✓
- **无循环依赖**：strategies/PM → execution 单向；port 侧依赖由 Phase 7 注入

## 5. 仍未迁移的 PM 职责（Phase 7 前保持现址）

open/close/partial 编排与状态机、`_POS_CACHE`/`pm:positions` 双写方、closed marker
生命周期、RECORD/PG/CH 记账、WS 三层加载与 `_merge_meta`、幽灵流、监控退出链、
Algo SL 队列与线程、sl/tp 管理、`_s6api` service locator、legacy `PM.open_position`。

## 6. 下一阶段建议

- P4-03-01-D2：AlgoPort / ObserverPorts（PG/TG）contract 化（同 D1 形态：callable 注入 + parity）
- SymbolMetaPort 收口（`_round_qty`/min_notional/funding → service）= step 04
- PM decomposition（state/persistence/monitoring 切片）= Phase 7
