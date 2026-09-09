# Execution Core (P4-02)

> 纯逻辑执行核心：从 `strategies/shared_executor.py` 与 `shared/position_manager.py`
> 的 Execution 路径提取，**零行为变化**（P4-01 的 60 个 characterization tests 全部原样通过；
> 125 个新增 core/parity tests 冻结提取结果）。

## 1. Core responsibility

`execution/core.py` 只负责 Execution 的**纯决策/纯数学**部分：

- **Side 映射**：持仓/信号方向 → 交易所订单方向
  （开仓 `order_side_for`：SHORT→SELL；平仓 `close_order_side_for`：SHORT→BUY——两个映射相反，均为真实代码逐字冻结）
- **Order Intent**：`OrderIntent`（frozen dataclass）+ 四种真实意图构造
  - `se_open_intent`：symbol/side/type/quantity/newOrderRespType=RESULT（无 positionSide/reduceOnly）
  - `pm_open_intent`：+ positionSide=BOTH
  - `close_intent`：+ positionSide=BOTH + reduceOnly='true'
  - `partial_close_intent`：+ positionSide=BOTH，**无 reduceOnly**（E-OBS-5 真实非对称）
- **Response parsing**：`is_rejected` / `parse_execution_result` → `ExecutionResult`
  （status/executedQty/cumQty/avgPrice 的 fallback、abs、异常语义逐行冻结；
  `is_rejected` 返回原始 truthy 值——`{}` 为 True、code 原值返回、code=0 为 falsy）
- **Fill classification**：`classify_open_fill` → `OpenFillOutcome`
  （UNFILLED_NEW → ZERO_FILL → PARTIAL_BELOW_HALF → ACCEPTED，真实 if 链顺序；
  阈值 0.01 / 严格小于 0.5，非新状态机）
- **Quantity normalization 纯核**（IO 留在调用方）：
  - `round_qty_by_lot_step` / `round_qty_from_exchange_info`（se LOT_SIZE 截断语义，
    含浮点伪影冻结：100.0 % 0.01 → 99.99）
  - `round_qty_by_precision`（pm 精度舍入核）
- **Close 纯数学**：`remaining_after_partial`（含 E-OBS-7 负数反向放大冻结）、
  `partial_pnl_u`、`position_pnl`（pnl_pct 不 round）、
  `has_remaining_position` / `accounted_close_qty`（0.001 阈值与 executedQty fallback）
- **Sandbox 谓词**：`is_order_path`（path 含 'order' 即拦截，含 algoOrder）

## 2. Core non-responsibility

以下全部**不在** Core（属于 shared_executor / PM / P4-03 Execution Service）：

- Binance API 调用（fapi_post/fapi_get）、签名、重试（当前无 retry）
- exchangeInfo 抓取、Redis（pm:positions / closed 标记）、PG 事件、TG 通知
- PM 注册 / position_id / record_trade / algo SL 队列与 worker
- sandbox 状态文件、mock 成交价格、`_sandbox_check` 开关
- open/close/partial 的编排顺序（分支体语句在 se 中逐字保留）
- leverage / marginType 下单、cancelOrder 发送

## 3. Dependency rule

```text
execution.core
    ↓
stdlib only（dataclasses / enum / typing / __future__）
```

验证：`tests/execution/test_execution_core.py::TestCoreDependencyRule`
（源码正则扫描 + 子进程干净 import 后检查 `sys.modules`）。
禁止依赖：redis / requests / binance / shared.position_manager /
strategies.* / shared_executor / psycopg / telegram / threading / queue / time。

## 4. Current extracted functions/models

| 类别 | 名称 | 来源 |
|---|---|---|
| 映射 | `order_side_for` | se L943 / PM L794 |
| 映射 | `close_order_side_for` | PM L1685 / L1798 |
| Intent | `OrderIntent` + `to_params` | 字段全部来自真实订单字面量 |
| Intent | `se_open_intent` | se L944-950 |
| Intent | `pm_open_intent` | PM L796-799 |
| Intent | `close_intent` | PM L1799-1801 |
| Intent | `partial_close_intent` | PM L1687-1690 |
| Parsing | `is_rejected` | se L951 |
| Parsing | `parse_execution_result` → `ExecutionResult` | se L956-960 |
| Fill | `OpenFillOutcome` / `classify_open_fill` | se L963-991 |
| Qty | `round_qty_by_lot_step` / `round_qty_from_exchange_info` | se L1058-1071 |
| Qty | `round_qty_by_precision` | PM L723-735 |
| Close | `remaining_after_partial` | PM L1697 |
| Close | `partial_pnl_u` | PM L1698-1699 |
| Close | `position_pnl` | PM L1723-1728 / L1868-1873 |
| Close | `REMAINING_EPS` / `has_remaining_position` / `accounted_close_qty` | PM L1818 / L1854 |
| Sandbox | `is_order_path` | se L144 |

## 5. Compatibility

```text
旧调用方式
    ↓
shared_executor（编排不变，compatibility 接线）
    ↓
execution.core
```

shared_executor.py 仅 4 处薄接线（分支体/阈值/异常语义逐字保留）：

1. `open_position`：订单 params ← `se_open_intent(...).to_params()`
2. `open_position`：解析 ← `parse_execution_result`；分支条件 ← `classify_open_fill`
   （rejection 检查保持原表达式；分支体内 cancelOrder/log 语句原样）
3. `_round_qty`：纯核 ← `round_qty_from_exchange_info`（IO/except 留在原处）
4. `_sandbox_post`：path 判定 ← `is_order_path`

PM **零改动**（其对应纯逻辑在 core 中有 parity 对照，接线属 Phase 7）。
P4-03 将以上述 Core 为唯一逻辑源，逐步替换 orchestration 并引入 IO adapters。
