# S7 Grid Architecture 优化路线图（2026-05）

## 当前架构

```text
s7_grid.py
    ↓
s7_runner.py
    ↓
s7_logic.py
    ↓
s7_core.py
```

职责：

```text
s7_grid
    启动入口

s7_runner
    主循环
    热更新检测
    生命周期管理

s7_logic
    策略逻辑

s7_core
    API
    WS
    配置
    状态持久化
    基础设施
```

整体架构合理。

当前主要问题已经不是策略，而是：

```text
可维护性
热更新一致性
状态管理
模块边界
```

------

# P0

## MarketGuard 热更新失效

### 现状

```python
core._guard = logic.MarketGuard(symbols)
core._guard.start()
```

仅启动一次。

------

热更新后：

```python
importlib.reload(logic)
```

策略逻辑已更新。

但是：

```text
旧 Guard 线程继续运行
```

不会使用新逻辑。

------

### 风险

修改：

```python
_judge_one()
```

后：

```text
manage_grid()
已更新

MarketGuard
未更新
```

产生逻辑不一致。

------

### 建议

方案A：

```text
Guard 下沉到 s7_core
```

作为基础设施。

不参与热更新。

------

方案B：

```text
reload 时重启 Guard
```

重新创建实例。

------

推荐：

```text
方案A
```

------

# P0

## RuntimeState 常驻内存

### 现状

```python
while True:

    state = load_state()

    ...

    save_state(state)
```

每轮巡检：

```text
读盘
写盘
```

------

### 问题

未来增加：

```text
WebUI
Backtest
PaperTrade
ClickHouse
```

容易发生：

```text
状态覆盖
状态回滚
```

------

### 建议

Runner 启动时：

```python
runtime_state = load_state()
```

之后：

```python
manage_grid(runtime_state)
```

全部在内存运行。

------

定时：

```python
save_state(runtime_state)
```

例如：

```text
每30分钟
退出前
重大状态变更
```

保存。

------

# P0

## 去掉 import *

### 现状

```python
from s7_core import *
```

------

### 问题

后期：

```text
函数来源不明确
名称污染
维护困难
```

------

### 建议

统一：

```python
import s7_core as core
```

使用：

```python
core.get_price()
core.get_atr()
core.get_ema()
```

------

# P1

## 拆分 manage_grid()

### 现状

manage_grid()

已经承担：

```text
Shock Filter
Market State
Inventory
Drawdown
Expectancy
Grid Build
Grid Update
PnL
```

------

### 风险

未来：

```text
1000+
2000+
行
```

必然出现。

------

### 建议

拆分：

```text
strategy/

├── shock_filter.py

├── market_regime.py

├── risk_engine.py

├── inventory_engine.py

├── expectancy_engine.py

├── grid_engine.py
```

------

主流程：

```python
shock_check()

risk_check()

inventory_check()

expectancy_check()

grid_update()
```

------

保持：

```text
单向依赖
```

------

# P1

## Context 对象

### 现状

大量函数直接访问：

```python
state
config
cache
api
```

------

### 建议

统一：

```python
class GridContext:
```

包含：

```python
state
config
market_cache
shock_cache
account_cache
runtime
```

------

函数签名：

```python
manage_grid(ctx, symbol)
```

而不是：

```python
manage_grid(symbol, state)
```

------

优势：

```text
回测可复用
模拟盘可复用
实盘可复用
```

------

# P1

## Inventory Exit 生命周期

### 现状

存在：

```python
inventory_exiting
```

判断。

------

缺失：

```text
开始时间
退出原因
结束状态
```

------

### 建议

统一：

```python
state["exit_state"][symbol]
```

结构：

```json
{
  "status": "aggressive",
  "reason": "drawdown",
  "start_time": 123456,
  "finished": false
}
```

------

禁止：

```text
退出过程中重建网格
```

------

# P1

## S2 Shock Score 标准化

### 现状

S7自行统计：

```python
shock =
signal_count
```

------

### 问题

信号数量：

```text
≠ 风险等级
```

------

### 建议

由 S2 输出：

```json
{
  "BTC": {
    "shock_score": 8.6
  },
  "ETH": {
    "shock_score": 6.4
  },
  "SOL": {
    "shock_score": 2.1
  },
  "market_heat": 12
}
```

------

S7直接消费：

```python
shock_score
```

不参与计算。

------

# P2

## Global Market Regime

### 目标

建立统一市场状态。

------

### 输出

```text
TREND
RANGE
CHAOS
```

------

定义：

```text
TREND
    强趋势

RANGE
    震荡

CHAOS
    高波动混乱
```

------

### 行为

TREND：

```text
S6 工作
S7 禁止新网格
```

------

RANGE：

```text
S7 工作
```

------

CHAOS：

```text
S6/S7 全部观望
```

------

### 价值

远高于继续优化：

```text
ATR_MULTIPLIER
GRID_STEP
GRID_LAYER
```

------

# P2

## Event Bus

### 当前

模块间：

```text
直接调用
```

------

建议

增加：

```python
publish_event()
```

例如：

```text
SHOCK_HIGH

GRID_PAUSED

EMERGENCY_EXIT

EXPECTANCY_NEGATIVE
```

------

未来：

```text
Telegram
WebUI
Dashboard
```

统一消费。

------

# P3

## 目录结构升级

建议演进为：

```text
s7/

├── runner.py

├── core/
│
├── exchange.py
├── market_data.py
├── persistence.py
├── state.py
│
├── strategy/
│
├── shock_filter.py
├── market_regime.py
├── risk_engine.py
├── inventory_engine.py
├── expectancy_engine.py
├── grid_engine.py
│
├── models/
│
├── context.py
├── events.py
├── types.py
│
└── state/
    runtime.json
```

------

# 最终优先级

## 第一阶段（必须）

```text
1. Guard 热更新问题
2. RuntimeState 常驻内存
3. 去掉 import *
4. 拆 manage_grid()
```

------

## 第二阶段（推荐）

```text
5. Context 对象
6. Inventory Exit 生命周期
7. Shock Score 标准化
```

------

## 第三阶段（长期）

```text
8. Global Market Regime
9. Event Bus
10. 完整模块化目录
```

------

# 总结

当前 S7 的策略层已经达到可实盘水平。

后续收益提升的关键不再是：

```text
网格间距
库存阈值
ATR参数
```

而是：

```text
什么时候允许网格运行
什么时候必须退出市场
```

因此未来研发重点应转向：

```text
Market Regime
Shock Pipeline
Runtime Architecture
```

而不是继续优化 Grid Engine 本身。