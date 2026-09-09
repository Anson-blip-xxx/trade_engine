# Execution Inventory（Phase 4-00 · READ-ONLY）

> 基于 feature/v2-architecture @ `1925155` 的实际代码逐行核实。
> 本阶段只记录现状，不实现、不重构、不修改任何生产逻辑。

---

## 1. Execution Entry Points

| function | file:line | 调用方 | 职责 | 层 |
|---|---|---|---|---|
| `shared_executor.open_position` | shared_executor.py:861-1056 | S6._open_long:293 / S8._open_short:274 | **主开仓入口**：Decision gates + Risk checks + Binance 下单 + PM 注册 + Algo SL + PG + TG | Decision+Risk+**Execution**+PM |
| `PM.open_position` | position_manager.py:751-835 | ⚠️ 当前**无生产调用方**（S6/S8 走 shared_executor 路径） | 简化版开仓（杠杆→保证金→MARKET→AlgoSL），无 Decision gates | **Execution only** |
| `PM._close` | position_manager.py:1704-1826 | `PM._monitor_one` 退出链 / `PM.close_position` | **主平仓入口**：防重入→沙盘/实盘→市价平→record→pg→清理 | **Execution+PM** |
| `PM._partial_close` | position_manager.py:1681-1701 | `PM._monitor_one` 分层止盈 | 部分平仓（reduceOnly MARKET） | **Execution** |
| `PM.close_position` | position_manager.py:1641-1652 | `maybe_replace_recovery_position` / 外部调用 | 外部平仓入口（包装 _close force=True） | **Execution** |
| `PM._monitor_one` | position_manager.py:1079-1260 | `pm_monitor`（S6/S8 主循环） | 11 步退出链（每步可能触发 _close/_partial_close） | PM/Risk |
| `PM._ghost_cleanup` | position_manager.py:842-868 | `monitor_all` Step 0 | 幽灵仓清理（交易所已平但 PM 未记录） | PM |
| `PM._algo_place_sl_inner` | position_manager.py:223-286 | `_algo_worker_loop`（11s 队列） | Algo 条件止损单下单 | **Execution** |
| `reconcile_positions` | shared_executor.py:332 | S6/S8 main 启动时 | 启动时从 Redis 恢复状态 | Infra |

**关键发现：两个 open_position 并存**：
- `shared_executor.open_position`（S6/S8 使用）：~200 行，含 Decision+Risk+Execution+PM+PG+TG
- `PM.open_position`（position_manager.py:751）：~85 行，仅 Execution（无 Decision/Risk gates）
- 两者调用**不同的 Binance API 实现**（shared_executor 用自带 fapi_post，PM 用 _s6api 的 fapi_post）

---

## 2. Open Flow（shared_executor.open_position 完整流程）

```
S6/S8._open_long/_open_short
  ↓ gates 链（Decision，12 道）
  ↓ score / leverage / stop / qty（Risk）
  ↓
shared_executor.open_position(name, symbol, side, entry_price, stop_price, qty, margin_mode, leverage, event_type, strength)
  │
  ├─ GATE 1: strength < 30 → False                     (:868)
  ├─ GATE 2: _analysis_gate → False/qty×penalty        (:872)
  ├─ GATE 3: R:R < 1.0 → False                         (:879)
  ├─ GATE 4: drawdown halt → False / reduced → qty×0.5 (:886)
  ├─ GATE 5: 4h 平仓冷却 → False                       (:894)
  ├─ GATE 6: PAUSE_OPEN 文件 → False                   (:898)
  ├─ GATE 7: min notional 调整                          (:904)
  ├─ GATE 8: funding 极值 → False                      (:913)
  ├─ GATE 9: 交易所已有仓位 → False                    (:922)
  │
  ├─ EXEC: POST /fapi/v1/leverage                       (:935)
  ├─ EXEC: POST /fapi/v1/marginType (ISOLATED/CROSSED)  (:936-939)
  ├─ EXEC: _round_qty (LOT_SIZE 精度)                   (:942)
  ├─ EXEC: POST /fapi/v1/order (MARKET, RESULT)         (:944)
  │
  ├─ PARSE: status/executedQty/cumQty/avgPrice          (:956-960)
  ├─ PARSE: 未成交 → 取消 + False                       (:963-970)
  ├─ PARSE: 成交量 0 → 取消 + False                     (:973-981)
  ├─ PARSE: 部分成交 (<50%) → 取消剩余，接受            (:984-991)
  │
  ├─ PM: _update_pos_cache → _POS_CACHE + pm:positions  (:995)
  │    └─ 失败 → 取消订单 + False                       (:997-1007)
  ├─ PG: OPEN_ORDER_FILLED event                        (:1013)
  ├─ TG: 开仓通知                                       (:1039)
  ├─ ALGO: _algo_enqueue(stop_price, filled_qty)        (:1047)
  │
  └─ return True
```

### 关键时序问题

| 问题 | 当前行为 | 风险 |
|---|---|---|
| **Binance 先于 PM** | 市价单已发出后才写 pm:positions | Binance 成功 + PM 缓存失败 → 取消订单（但 MARKET 单可能已部分成交无法取消） |
| **重复 open** | 订单发出后才检查 symbol 是否已在持仓 → 已在则返回 True 但**不取消订单** | 意外双倍敞口（P1-04 OBS-3） |
| **Algo SL 异步** | 止损单通过 11s 队列异步挂载 → 市价单成交到 AlgoSL 挂上之间有**无止损窗口** | 市场极速波动时仓位裸奔 |
| **partial fill 取消** | MARKET 单部分成交 <50% → 尝试取消剩余 → 但 MARKET 单通常立即全部成交 | 取消是防御性的，MARKET 单不太可能需要 |

---

## 3. Close Flow（PM._close 完整流程）

```
PM._close(symbol, pos, price, reason, positions, force=False)
  │
  ├─ GATE: force=False + closed:symbol 标记 → False（防重入）
  ├─ _mark_closed(symbol)                                ← 立即标记（先标记后平仓）
  │
  ├─ SANDBOX PATH (_sandbox_active == True):
  │    sandbox._close_position(symbol)
  │    → pnl 计算 (LONG/SHORT 公式)
  │    → record_trade (final_close=True)
  │    → positions.pop(symbol)
  │    → _save(positions)
  │    → return True
  │
  ├─ REAL PATH:
  │    ├─ fapi_get positionRisk → 确认实盘持仓
  │    ├─ if 无持仓（交易所已平）:
  │    │     → _cancel_all_algo
  │    │     → pnl 计算
  │    │     → record_trade (final_close=True)
  │    │     → _pg_record_event(EXCHANGE_POSITION_FLAT)
  │    │     → positions.pop + _save
  │    │     → return True
  │    │
  │    ├─ fapi_post MARKET reduceOnly close_side
  │    │     → if code → _log_close_error + _clear_closed_marker + return False
  │    │
  │    ├─ fapi_get positionRisk again → 确认剩余
  │    ├─ if remaining >= 0.001（部分成交）:
  │    │     → record_trade (final_close=False, qty=filled)
  │    │     → _pg_record_event(CLOSE_ORDER_PARTIAL)
  │    │     → positions[symbol].qty = remaining
  │    │     → _save
  │    │     → return False（等待下轮继续平）
  │    │
  │    ├─ 全部成交:
  │    │     → _cancel_all_algo
  │    │     → pnl 计算
  │    │     → _pg_record_event(CLOSE_ORDER_FILLED)
  │    │     → record_trade (final_close=True)
  │    │     → positions.pop + _save
  │    │     → return True
```

### 关键时序问题

| 问题 | 当前行为 | 风险 |
|---|---|---|
| **先标记后平仓** | `_mark_closed` 在市价单之前 → 平仓失败时 `_clear_closed_marker` 恢复 → 窗口内其他进程跳过平仓 | 极端情况下该 symbol 失去平仓保护 |
| **部分成交语义** | MARKET 单部分成交 → 返回 False → 下轮 _close 重试剩余部分 | 依赖轮询循环的可靠性 |
| **交易所已平路径** | positionRisk 无持仓 → 直接记账（不取消订单）→ **可能遗漏未取消的止损单** | Algo SL 残留可能在未来触发意外平仓 |

---

## 4. Order Construction

### shared_executor.open_position 的订单参数

| 字段 | 值 | 来源 |
|---|---|---|
| symbol | 方法参数 | S6/S8 |
| side | 'BUY' (LONG) / 'SELL' (SHORT) | side 参数映射 |
| type | 'MARKET' | 硬编码 |
| quantity | _round_qty 后的 qty | Risk 层输出 + LOT_SIZE 精度 |
| newOrderRespType | 'RESULT' | 硬编码（同步获取成交） |
| positionSide | 'BOTH' | **未设置**（Binance 默认 BOTH） |

### PM._close 的平仓订单参数

| 字段 | 值 | 来源 |
|---|---|---|
| symbol | 方法参数 | PM |
| side | 'BUY' (平 SHORT) / 'SELL' (平 LONG) | pos.side 取反 |
| type | 'MARKET' | 硬编码 |
| quantity | _round_qty 后的 abs(positionAmt) | 交易所实际持仓 |
| positionSide | 'BOTH' | 硬编码 |
| reduceOnly | 'true' | 硬编码 |

### PM.open_position 的订单参数（legacy）

| 字段 | 值 | 备注 |
|---|---|---|
| symbol / side / type=MARKET / quantity | 同上 | 无 newOrderRespType（默认 ACK） |
| 无 reduceOnly | — | 开仓方向 |

### Algo SL（PM._algo_place_sl_inner）

- 使用 `/fapi/v1/algoOrder` API
- side：BUY (平 SHORT) / SELL (平 LONG)
- triggerPrice / quantity
- 11s 队列间隔（Binance rate limit）

---

## 5. Binance API Dependencies

### 四套 Binance API 实现

| 实现 | 位置 | 沙盘拦截 | 健康打点 | 使用方 |
|---|---|---|---|---|
| **A: shared/binance_api.py** | :72-126 | ✅ `_sandbox_intercept` | ✅ `health.record` | S3、S0、tv_bridge、sentiment_bridge |
| **B: shared_executor 内部** | :149-181 | ✅ `_sandbox_post/_get` | ❌ 无 | S6/S8 全部交易调用 |
| **C: PM _light_fapi_*** | position_manager.py:72-156 | ❌ 无 | ❌ 无 | PM 全部（Algo SL、查仓、平仓） |
| **D: s7_core.py** | services/s7/s7_core.py | ❌ 无 | ❌ 无 | S7（DEFER） |

**沙盘拦截覆盖**：
- A 拦截：`/fapi/v2/positionRisk`、`/fapi/v2/account`、`/fapi/v1/order`、`/fapi/v1/openOrders`、Algo
- B 拦截：`order`、`positionRisk`、`account`、`openOrders`、`algo`
- C 不拦截：PM 的平仓/查仓在沙盘模式下走 `_sandbox_active()` 前置判断（不是 API 拦截）
- D 不拦截：S7 自己管理

### Binance API 端点清单（当前实际使用）

| 端点 | 方法 | 调用方 | 用途 |
|---|---|---|---|
| `/fapi/v1/leverage` | POST | shared_executor:935, PM:783 | 设杠杆 |
| `/fapi/v1/marginType` | POST | shared_executor:937, PM:789 | 设保证金模式 |
| `/fapi/v1/order` | POST | shared_executor:944, PM:796, PM:1799, PM._partial_close:1689 | MARKET 开/平仓 |
| `/fapi/v1/order` (cancel) | POST | shared_executor:966-988, PM:~1750 | 取消未成交订单 |
| `/fapi/v2/positionRisk` | GET | shared_executor:923, has_any_position:424, PM:1749, PM:1812 | 查仓 |
| `/fapi/v1/exchangeInfo` | GET | shared_executor._round_qty:1061, _get_min_notional:84 | LOT_SIZE / MIN_NOTIONAL |
| `/futures/data/globalLongShortAccountRatio` | GET | get_short_ratio:694 | 多空比 |
| `/fapi/v1/algoOrder` | POST | PM._algo_place_sl_inner | Algo SL |
| `/fapi/v1/algoOrder` (delete) | DELETE | PM._cancel_all_algo | 取消 Algo SL |
| `/fapi/v1/income` | GET | trade_recorder | REALIZED_PNL |

---

## 6. PositionManager Interaction

### Execution 前后 PM 操作时序

```
open_position (shared_executor):
  [BEFORE] 无 PM 操作（Decision gates 检查 PM 缓存 get_position_count/has_any_position）
  [ORDER]  Binance MARKET
  [AFTER]  _update_pos_cache → 写 _POS_CACHE + _rset('pm:positions')
           失败 → 取消订单（但 MARKET 可能已部分成交）

PM._close:
  [BEFORE] _mark_closed(symbol) → _rset('closed:symbol')
           （防止其他进程重复平仓）
  [ORDER]  Binance MARKET reduceOnly
  [AFTER]  positions.pop + _save
           record_trade → PG + CH
           _pg_record_event → trade_events
           _cancel_all_algo

PM._partial_close:
  [BEFORE] 无标记（不防重入）
  [ORDER]  Binance MARKET reduceOnly (partial qty)
  [AFTER]  pos['qty'] -= close_qty
           _save(positions)
           （无 record_trade — PnL 不单独记账）
```

### PM / Exchange 状态不一致窗口

| 窗口 | 触发条件 | 后果 |
|---|---|---|
| open: Binance 成交 → PM 缓存更新失败 | _update_pos_cache 抛异常 | 取消订单（MARKET 可能已不可取消）→ 交易所有仓但 PM 不知道 → 幽灵仓 |
| open: 同 symbol 重复 open | 市价单已发出 → 发现已有仓 → 不覆盖记录 | 交易所实际仓位 > PM 记录 |
| close: 标记后平仓失败 | _clear_closed_marker 恢复，但窗口内其他进程跳过 | 平仓延迟 |
| close: 部分成交后进程崩溃 | positions[symbol].qty 已减少但未 _save | PM 认为还有更多仓位，实际交易所只剩剩余 |
| ghost: 交易所已平但 PM 不知道 | Algo SL 在交易所触发（PM 未感知） | 幽灵仓 → _ghost_cleanup 处理 |
| PM: WS/REST/meta 三层不一致 | WS 延迟或 REST 超时 | 短暂不一致，由 _merge_meta 的 preserve_missing 机制兜底 |

---

## 7. Error / Retry Behavior

### shared_executor.open_position

| 错误 | 处理 | 恢复 |
|---|---|---|
| 下单 API 异常 | try/except → log → return False | 无重试，等下轮事件 |
| 订单 rejected (code) | log → return False | 无重试 |
| MARKET 未成交 (NEW) | cancel → return False | 无重试 |
| 成交量 0 | cancel → return False | 无重试 |
| 部分成交 <50% | cancel 剩余 → 继续处理已成交部分 | 接受部分 |
| PM 缓存更新异常 | cancel 订单 → return False | 无重试（但 MARKET 可能已成交无法取消） |
| 整体异常 | log → return False | 无重试 |

### PM._close

| 错误 | 处理 | 恢复 |
|---|---|---|
| 交易所拒绝 (code) | _log_close_error + _clear_closed_marker → return False | 下轮 _close 重试（marker 已清除） |
| 部分成交 | 保留剩余仓位 → return False | 下轮 _close 继续平 |
| API 异常 | _clear_closed_marker → return False | 下轮重试 |
| 防重入 | return False（跳过） | 4h 后解除 |
| _log_close_error | 60s 去重（避免刷屏） | — |

### PM._partial_close

| 错误 | 处理 | 恢复 |
|---|---|---|
| 交易所拒绝 | log → return（不更新 qty） | 下次触发重试 |
| API 异常 | log → return | 下次触发重试 |

### 静默吞掉

| 位置 | 吞掉的内容 | 风险 |
|---|---|---|
| `_get_min_notional` | exchangeInfo 查询异常 → 返回 5.0 | min_notional 可能不准确 |
| `_get_funding_rate` | 费率查询异常 → 返回 0 | 费率 gate 可能误放行 |
| `_round_qty` | exchangeInfo 查询异常 → 原始 qty | 精度可能不符 |
| `has_any_position` 兜底 | positionRisk 查询异常 → False | 可能误放行 |
| `PM._save` | Redis 异常 → 静默 | 持仓变更可能丢失 |
| `PM._mark_closed` | Redis 异常 → 静默 | 防重入标记可能丢失 |
| `PM._clear_closed_marker` | Redis 异常 → 静默 | 标记可能残留 |

---

## 8. Sandbox Guards

| # | 位置 | 拦截方式 | 覆盖范围 |
|---|---|---|---|
| 1 | `shared/binance_api.py:54-76` | 函数级拦截（fapi_get/fapi_post/fapi_delete 前置检查） | positionRisk / account / order / openOrders / algo |
| 2 | `strategies/shared_executor.py:129-165` | 独立函数级拦截（自带 fapi_post/fapi_get 前置检查） | order / positionRisk / account / openOrders |
| 3 | `shared/position_manager.py:33-39` | 前置布尔判断（`_sandbox_active()`） | PM._close / PM.open_position / PM._ghost_cleanup |
| 4 | `strategies/s6_auto_trader.py` | 遗留（legacy 路径不再活跃） | — |

**重复 guard**：拦截点 1 和 2 功能等价但实现独立——shared_executor 的 fapi_post 在调用 Binance 前先查自己的 sandbox，但如果通过 binance_api 的 fapi_post 则查 binance_api 的 sandbox。**同一次下单可能被检查两次**（无害但冗余）。

**沙盘模式未覆盖的路径**：
- PM._light_fapi_* 不经过沙盘拦截（直接绕过）——但 PM._close 有前置 `_sandbox_active()` 布尔判断兜底
- Algo SL worker（PM）在沙盘模式下仍会入队，但 PM._algo_place_sl_inner 用 _light_fapi_post → 不走沙盘 → 可能真实下单（⚠️ 但 demo 环境无实际影响）

---

## 9. Redis / State Dependencies

### Execution 直接访问的 Redis key

| Key | 操作 | 函数 | 层 |
|---|---|---|---|
| `pm:positions` | W | `_update_pos_cache` / PM._save | PM 状态 |
| `closed:{symbol}` | R/W | `_was_closed_recently` / PM._mark_closed / PM._clear_closed_marker | Decision/PM |
| `pm:monitor:writer` | Lock | pm_monitor（S6/S8 主循环） | PM |
| `pm:ghost_close:{sym}` | Lock | PM._ghost_cleanup | PM |
| `account:peak` | R/W | _drawdown_status | Risk |
| `account:dd_pause` | R/W | _drawdown_status | Risk |
| `market:s0` | R | market_allows_trading | Decision |
| `state:s6` / `state:s8` | R/W | load_state/save_state | Strategy state |
| `cd:loss` | R/W | trade_recorder | 冷却 |
| `event:analysis_reject` | W | _record_analysis_decision | Decision |
| `alert:external_position:*` | W | PM._notify_external_position | PM |

### 状态归属

| 状态 | 真正属于 | 当前位置 |
|---|---|---|
| 持仓记录 | PM | `pm:positions`（经 shared_executor._update_pos_cache 写入） |
| 平仓标记 | Decision/PM | `closed:{symbol}` |
| 回撤状态 | Risk | `account:peak/dd_pause` |
| 市场状态 | Decision (S0) | `market:s0` |
| 冷却状态 | Decision | `state:s6/s8` 里的 cooldowns |
| Algo SL 队列 | Execution | 进程内存 `_ALGO_QUEUE`（非 Redis） |

---

## 10. S6 / S8 Differences

### Execution 层（shared_executor.open_position）

- **完全共享**：同一个 open_position 函数，S6/S8 调用方式完全相同
- **唯一差异**：调用方传入的参数不同（side, event_type, system_tag）

### PM 退出链

- **结构相同**：11 步 _monitor_one
- **SYSTEM_CFG 不同**：
  - S8A: be_done 2%, partial {5:0.3}, peak_guard 3%/2%, time_stop 240min
  - S6A: be_done 2.5%, partial {5:0.5}, peak_guard 3%/2%, time_stop 120min
  - S6B: be_done 5%, partial {8:0.3}, peak_guard 5%/3%, time_stop 480min, trail 0.6×ATR

### 开仓参数差异

| 参数 | S6 | S8 |
|---|---|---|
| STOP_LOSS_PCT 基线 | PULSE_UP:0.04, 其余:0.06 | PULSE_DOWN:0.04, PANIC_SELL:0.035, 其余:0.05 |
| MARGIN_MODE | PULSE_UP:ISOLATED, PUMP_UP:ISOLATED, 其余:CROSSED | PULSE_DOWN:ISOLATED, PANIC_SELL:ISOLATED, PUMP_DOWN:ISOLATED, 其余:CROSSED |
| LEVERAGE | PULSE_UP:5, PUMP_UP:2, 其余:3 | PULSE_DOWN:5, PANIC_SELL:5, PUMP_DOWN:2, 其余:3 |
| takeover 分支 | 有（S6B） | 无 |
| regime gate | 有（VIOLENT_BULLISH） | 无 |
| strength gate | 无（仅 open_position 内 30） | 有（≥60 门槛） |
| pump_guard | 无 | 有 |

---

## 11. shared_executor Coupling

open_position 对 shared_executor 内部的依赖清单：

| 依赖 | 类型 | 用途 | 提取影响 |
|---|---|---|---|
| `_fapi_sig/fapi_get/fapi_post` | 函数 | Binance API 调用 | 需注入 exchange adapter |
| `_sandbox_check/_sandbox_post/_sandbox_get` | 函数 | 沙盘拦截 | 需注入 sandbox adapter |
| `_SANDBOX_ACTIVE` | 模块变量 | 沙盘状态缓存 | 全局状态 |
| `_get_min_notional` | 函数 | 交易所 LOT_SIZE/MIN_NOTIONAL | 需注入 |
| `_get_funding_rate` | 函数 | 费率查询 | 需注入 |
| `_round_qty` | 函数 | LOT_SIZE 精度 | 需注入 |
| `_analysis_gate` | 函数 | 历史分析过滤 | 需注入 |
| `_drawdown_status` | 函数 | 回撤熔断 | 需注入 |
| `_was_closed_recently` | 函数 | 4h 重开冷却 | 需注入 |
| `_update_pos_cache` | 函数 | PM 注册（写 _POS_CACHE + pm:positions） | 需注入 |
| `_algo_enqueue/_algo_start_worker` | 函数 | Algo SL 队列 | 需注入 |
| `_pg_record_event` | 函数 | PG 事件记录 | 需注入 |
| `_log` | 函数 | 日志 | 需注入 |
| `tg_fn` | 参数 | Telegram | 调用方传入 |
| `_MIN_RR` | 常量 | R:R 最低门槛 | 常量迁移 |
| `_POSITION_MIN_USDT` | 常量 | min notional floor | 常量迁移 |
| `_POS_CACHE` | 模块变量 | 持仓缓存 | 全局状态（module identity 风险） |

---

## 12. Current Execution State Machine

```
状态: IDLE → GATE_CHECK → ORDER_SENT → FILLED → REGISTERED → ALGO_PENDING
                    │                                          │
                    ▼                                          ▼
               REJECTED                                   MONITORING
                                                               │
                                              ┌────────────────┤
                                              ▼                ▼
                                         PARTIAL_CLOSE     FULL_CLOSE
                                              │                │
                                              ▼                ▼
                                         (回到 MONITORING)  CLOSED
```

```
CLOSE 状态机:
  IDLE → MARKED → EXCHANGE_CHECK → ORDER_SENT → CONFIRMED_FLAT → CLOSED
                      │                               │
                      ▼                               ▼
                 REJECTED                        PARTIAL_FILLED
                      │                               │
                      ▼                               ▼
              MARKER_CLEARED                    (回到 EXCHANGE_CHECK)
              (回到 IDLE)
```

---

## 13. Proposed Execution Boundary

Phase 4 应从 shared_executor.py 中提取的 Execution 边界：

**属于 Execution（Phase 4 提取）**：
- Binance 下单调用（MARKET / Algo SL / cancel）
- 成交结果解析（avgPrice / filledQty / partial fill 判定）
- LOT_SIZE 精度处理（_round_qty）
- 杠杆/保证金模式 API
- exchangeInfo 查询（LOT_SIZE / MIN_NOTIONAL）

**不属于 Execution（已在/将在其他 Phase 处理）**：
- Decision gates（strength30/analysis/R:R/熔断/4h冷却/PAUSE/费率/交易所查仓）→ Phase 3-02 已建 Decision Core，Phase 3-04 建 Risk
- PM 注册（_update_pos_cache）→ PM 边界
- Telegram 通知 → Notification
- PG 事件 → Persistence
- Algo SL 队列编排 → Execution（但队列线程管理属 Infra）

**Phase 4 Execution 应该接收的输入**：
```python
ExecutionIntent:
  symbol: str
  side: str          # BUY / SELL
  quantity: float
  leverage: int
  margin_mode: str   # ISOLATED / CROSSED
  stop_price: float
```

**Phase 4 Execution 应该返回的输出**：
```python
ExecutionResult:
  success: bool
  filled_qty: float
  avg_price: float
  order_id: str | None
  status: str        # FILLED / PARTIAL / REJECTED / CANCELLED
```

---

## 14. Risky / Ambiguous Behaviors

| # | 行为 | 锁定测试 | 风险 |
|---|---|---|---|
| E-1 | 同 symbol 重复 open：订单已发出但记录不覆盖 | P1-04 test_open_same_symbol | 双倍敞口 |
| E-2 | MARKET 成交后 PM 缓存失败 → 取消订单 | 无直接测试（依赖 _update_pos_cache 异常） | 可能无法取消已成交部分 |
| E-3 | Algo SL 异步挂载（11s 队列）→ 成交到挂 SL 之间无止损 | P1-04 test_open（algo_sl_id=None） | 极端波动时裸奔 |
| E-4 | PM._close 先标记后平仓，失败清标记 | P1-04 test_close_post_rejected | 窗口内其他进程跳过 |
| E-5 | PM._partial_close 无防重入标记 | P1-04（无测试锁定此行为） | 可能重复部分平仓 |
| E-6 | 交易所已平路径不取消 Algo SL | 无测试 | Algo SL 残留可能触发 |
| E-7 | MARKET 单用 RESULT 模式但 Binance 可返回 NEW | 无测试 | 低流动性币种可能挂单 |
| E-8 | 部分成交 <50% 时取消剩余 | 无测试 | 取消可能失败（已全部成交） |
| E-9 | PM._partial_close 无防重入标记 | 无测试 | 可能重复部分平仓 |
| E-10 | 两套 Binance API（shared_executor vs PM）的沙盘拦截不一致 | Phase 0 确认 | 沙盘行为可能不同 |

---

## 15. P4-01 Characterization Test Checklist

Phase 4-01 应冻结的测试清单（按优先级）：

### A. open_position 成交流程
- [ ] 正常全量成交：返回 True，订单参数冻结
- [ ] 部分成交 <50%：取消剩余 + 记录已成交部分
- [ ] 部分成交 ≥50%：接受全部
- [ ] MARKET 未成交 (NEW)：取消 + False
- [ ] 成交量 0：取消 + False
- [ ] 订单 rejected：False

### B. open_position Gate 路径
- [ ] strength < 30 → False
- [ ] analysis hard block → False
- [ ] analysis soft → qty 缩减后继续
- [ ] R:R < 1.0 → False
- [ ] R:R == 1.0 → 通过
- [ ] drawdown halt → False
- [ ] drawdown reduced → qty 缩减
- [ ] 4h 重开冷却 → False
- [ ] PAUSE_OPEN → False
- [ ] funding 极值 SHORT/LONG → False
- [ ] 交易所已有仓位 → False
- [ ] min notional 调整

### C. PM._close 路径
- [ ] 防重入（近期已平仓）
- [ ] force 绕过防重入
- [ ] 沙盘路径（pnl 方向 LONG/SHORT）
- [ ] 交易所已平（EXCHANGE_POSITION_FLAT）
- [ ] 市价平仓成功
- [ ] 市价平仓拒绝 → 清标记
- [ ] 部分成交 → 保留剩余
- [ ] 多持仓隔离

### D. PM._partial_close 路径
- [ ] LONG 方向 pnl
- [ ] SHORT 方向 pnl
- [ ] 连续 partial
- [ ] close_qty=0 / 负数（当前行为冻结）

### E. Algo SL 队列
- [ ] enqueue 正确入队
- [ ] worker 11s 间隔
- [ ] place_sl_inner 参数冻结

### F. _round_qty
- [ ] LOT_SIZE 精度
- [ ] fallback 6dp

### G. 沙盘拦截
- [ ] binance_api 拦截命中
- [ ] shared_executor 拦截命中
- [ ] 双重拦截无害验证
