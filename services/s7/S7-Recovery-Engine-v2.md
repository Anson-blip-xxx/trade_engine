```text
# S7 Recovery Engine V2（正式版）

## 背景

当前 S7 已具备：

​```text
Shock Filter
Inventory Drawdown
Expectancy Engine
Regime ATR
MarketGuard
Risk-Off
​```

实盘验证结果：

​```text
ETH  -> Risk-Off
BTC  -> Risk-Off
SOL  -> Weak Bear

Inventory = 0
​```

说明系统已经具备：

​```text
识别趋势风险
暂停网格
清空库存
避免接飞刀
​```

能力。

---

当前最大问题：

​```text
如何恢复运行
​```

而不是：

​```text
如何继续逃跑
​```

---

# 设计目标

建立完整闭环：

​```text
RANGE

 ↓

WEAK_BEAR

 ↓

RISK_OFF

 ↓

WATCH

 ↓

RANGE
​```

---

目标：

​```text
尽量早退出

更重要的是

尽量早恢复
​```

---

# 状态机设计

## RANGE

正常运行状态。

允许：

​```text
双边网格
新增挂单
重建网格
​```

---

## WEAK_BEAR

风险预警状态。

允许：

​```text
保留现有网格
减少新增买单
​```

---

## RISK_OFF

高风险状态。

行为：

​```text
停止新网格
停止新增买单
允许库存退出
​```

---

## WATCH

恢复观察状态。

行为：

​```text
不重建网格
持续观察市场恢复情况
等待确认
​```

---

# Risk-Off Snapshot

## 设计目标

Recovery Score 必须使用固定基准。

禁止：

​```text
事后推算 crash_atr
事后推算 crash_volume
​```

---

## 进入 Risk-Off 时

记录：

​```python
state["recovery"][symbol]["snapshot"]
​```

结构：

​```json
{
  "price": 92000,
  "atr": 5000,
  "volume": 1200000,
  "shock": 8.5,
  "timestamp": 1749000000
}
​```

---

说明：

​```text
整个恢复周期内
快照只读
不修改
​```

---

仅在：

​```text
重新回到 RANGE
​```

后更新。

---

# Recovery Score

范围：

​```text
0 ~ 10
​```

---

## 1. Shock 降温

来源：

​```python
shock_score
​```

规则：

​```text
shock < 3
+3

shock 3~5
+1

shock > 5
0
​```

---

## 2. ATR 收缩

计算：

​```python
atr_ratio =
current_atr /
snapshot.atr
​```

规则：

​```text
atr_ratio < 0.5
+2

atr_ratio < 0.7
+1
​```

---

## 3. EMA 收敛

计算：

​```python
ema_gap =
abs(ema20 - ema60)
​```

规则：

​```text
较 Risk-Off 时下降 30%
+2
​```

---

## 4. 成交量回归

计算：

​```python
volume_ratio =
current_volume /
snapshot.volume
​```

规则：

​```text
volume_ratio < 0.5
+2

volume_ratio < 0.7
+1
​```

---

## 5. 时间衰减

规则：

​```text
Risk-Off 超过24小时
+1
​```

---

## 6. 价格恢复（新增）

计算：

​```python
price_recovery =
(current_price - snapshot.price)
/
snapshot.price
​```

规则：

​```text
price_recovery > 5%
+1
​```

---

说明：

​```text
ATR下降
不一定代表市场恢复

价格企稳
更能确认恢复
​```

---

# 状态切换

## RISK_OFF → WATCH

条件：

​```python
recovery_score >= 4
​```

---

行为：

​```text
记录 watch_start
发送通知
​```

---

状态记录：

​```json
{
  "state": "WATCH",
  "watch_start": 1749001000
}
​```

---

通知：

​```text
BTC

Risk-Off → Watch

Recovery Score: 4.8

进入恢复观察阶段
​```

---

# WATCH → RANGE

## 条件

​```python
recovery_score >= 7
​```

并且：

​```python
now - watch_start >= 15分钟
​```

---

说明：

采用：

​```text
时间确认
​```

而非：

​```text
连续3轮巡检
​```

---

原因：

​```text
重启安全
热更新安全
巡检频率无关
​```

---

# 状态防抖

增加：

​```python
state_enter_time
​```

---

规则：

​```text
每个状态至少停留30分钟
​```

避免：

​```text
RANGE
↓
WATCH
↓
RANGE
↓
WATCH
​```

频繁切换。

---

# TG 输出增强

当前：

​```text
BTC risk-off
ETH risk-off
SOL weak_bear
​```

---

改为：

​```text
BTC

State: WATCH

Recovery Score: 5.8

Risk-Off: 18h

ATR Ratio: 0.62

Volume Ratio: 0.58
​```

---

# 持久化结构

​```json
{
  "BTC": {
    "state": "WATCH",

    "state_enter_time": 1749000000,

    "risk_off_start": 1748990000,

    "watch_start": 1749001000,

    "score": 5.8,

    "snapshot": {
      "price": 92000,
      "atr": 5000,
      "volume": 1200000,
      "shock": 8.5,
      "timestamp": 1748990000
    }
  }
}
​```

---

# 实施顺序

## Phase 1

实现：

​```text
Risk-Off Snapshot
Recovery Score
WATCH状态
TG通知
​```

---

## Phase 2

实现：

​```text
WATCH → RANGE
时间确认机制
状态防抖
​```

---

## Phase 3

实现：

​```text
Price Recovery
全市场 Recovery Engine
Global Recovery Score
​```

---

# 核心原则

S7 不负责：

​```text
预测趋势
追逐趋势
趋势开仓
​```

这些属于 S6。

---

S7 负责：

​```text
发现震荡

利用震荡

避开趋势

趋势结束后最快恢复运行
​```

---

最终目标：

​```text
不是比别人更早逃跑

而是在安全前提下

比别人更早回来
​```xxxxxxxxxx 不是比别人更早逃跑而是在安全前提下比别人更早回来text
```