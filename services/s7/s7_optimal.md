# S7 Adaptive Grid 优化方案（V2）

## 目标

将当前 S7 从：

```text
自适应网格
```

升级为：

```text
状态驱动网格 + OI风险过滤 + 库存风控 + 动态做市
```

核心原则：

```text
S2 负责发现趋势
S6 负责跟随趋势
S7 负责利用震荡
```

S7 不追求抓趋势。

S7 的目标：

```text
避免趋势
利用震荡
控制库存
稳定收租
```

------

# P0（最高优先级）

## 1. S2 Shock Filter 接入

### 问题

当前 S7 只能识别：

```text
趋势已经形成
```

无法识别：

```text
趋势即将启动
```

这是网格最大风险来源。

------

### 方案

读取 S2 输出：

```json
{
  "BTC": 7.8,
  "ETH": 6.5,
  "SOL": 3.2,
  "market_heat": 11
}
```

------

### 新状态

```text
GRID
WATCH
RISK_OFF
```

------

### 规则

```python
if shock_score >= 6:
    state = WATCH

if shock_score >= 8:
    state = RISK_OFF

if market_heat >= 10:
    global_pause_grid = True
```

------

### 行为

WATCH：

```text
停止新增网格
保留已有卖单
```

RISK_OFF：

```text
撤销所有网格
启动库存退出
```

------

# P0

## 2. Inventory Drawdown Engine

### 问题

当前库存管理只看：

```python
inventory/max_inventory
```

无法识别深度套牢。

------

### 增加

```python
inventory_drawdown
```

计算：

```python
(price - avg_cost) / avg_cost
```

------

### 风控规则

```python
DD < -10%
    减少50%买单

DD < -15%
    停止补仓

DD < -20%
    主动减仓

DD < -25%
    强制清仓
```

------

# P0

## 3. Expectancy Engine

### 问题

系统不知道网格是否赚钱。

------

### 增加统计

```python
cycle_count
cycle_profit
```

------

### 指标

```python
expectancy =
cycle_profit / cycle_count
```

------

### 规则

```python
expectancy < 0
```

连续：

```python
30 cycles
```

则：

```python
暂停该币网格
```

------

# P1

## 4. Grid Profitability Upgrade

当前：

```python
step = ATR * 0.5
```

------

改为：

```python
step =
ATR
× RegimeFactor
```

------

RegimeFactor：

```python
trend:
    0.3

range:
    0.8

risk:
    1.2
```

------

目的：

```text
趋势行情缩窄网格
震荡行情放宽网格
```

------

# P1

## 5. Market Heat Engine

由 S2 输出：

```python
market_heat
```

统计：

```text
OI Shock Count
Funding Anomaly Count
Trade Burst Count
```

------

规则：

```python
market_heat < 3
```

适合网格。

------

```python
market_heat > 10
```

暂停全部网格。

------

# P1

## 6. Event Driven Guard

### 问题

当前：

```text
Guard
↓
等待巡检
↓
处理
```

最大延迟：

```text
5分钟
```

------

### 改造

Guard 直接触发：

```python
crash
```

立即：

```python
cancel_all_orders()

market_close()
```

无需等待下一轮巡检。

------

# P2

## 7. Dynamic Market Making Bias

### 当前

买卖单完全对称：

```text
50%
50%
```

------

### 增加

Orderbook Imbalance

```python
imbalance =
bid_depth / total_depth
```

------

规则：

```python
imbalance > 0.65
```

调整：

```python
BUY 70%
SELL 30%
```

------

```python
imbalance < 0.35
```

调整：

```python
BUY 30%
SELL 70%
```

------

目的：

```text
顺势做市
而非死板挂单
```

------

# P2

## 8. Inventory Exit Lock 修复

当前：

```python
inventory_exiting
```

存在入口。

无完整生命周期。

------

增加：

```python
inventory_exit_start_time
inventory_exit_reason
inventory_exit_finished
```

------

退出完成前：

```python
禁止重建网格
```

------

# P3

## 9. 增量更新修复

当前：

```python
float(price)
```

作为字典 Key。

存在：

```text
浮点误差
```

风险。

------

改为：

```python
Decimal
```

或者：

```python
str(price)
```

------

# P3

## 10. 状态持久化修复

全量重建网格时：

```python
avg_cost
realized_pnl
inventory_qty
```

可能丢失。

------

统一：

```python
state['grids'][symbol] = {
    **grid_info,
    ...
}
```

------

# 最终架构

```text
S2
│
├── Shock Score
├── Market Heat
└── Risk Signal
      │
      ▼

S7
│
├── State Machine
├── Grid Engine
├── Inventory Engine
├── Expectancy Engine
├── Exit Engine
└── Market Making Bias

      │
      ▼

稳定收租
避免趋势
控制库存
```

------

# 实施顺序

Phase 1

```text
1. S2 Shock Filter
2. Inventory Drawdown Engine
3. Expectancy Engine
```

预计收益提升最大。

------

Phase 2

```text
4. Market Heat
5. Event Driven Guard
6. Regime ATR
```

提高生存能力。

------

Phase 3

```text
7. Dynamic Market Making
8. Exit Lock
9. 浮点修复
10. 状态持久化
```

提高长期稳定性。