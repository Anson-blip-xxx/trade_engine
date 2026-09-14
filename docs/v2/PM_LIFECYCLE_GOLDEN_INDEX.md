# PM Lifecycle Golden Index — P7-07A

> 冻结 `open_position` / `_close` / `close_position` / `_partial_close` /
> exchange-flat close 的完整 side-effect 排序与失败拓扑，作为 P7-07B
> Lifecycle Service 抽取的迁移约束。**生产代码零改动。**

## Call Graph（当前 HEAD 真实提取）

### Open（`open_position`）
```
_was_closed_recently guard → False（无 Binance side effect）
fapi_post leverage（异常吞错 log）
fapi_post marginType（异常吞错 log）
fapi_post MARKET order（异常 → '[开仓失败]' → False，无 state）
PMB-30: _algo_start_worker() + _algo_enqueue(sl)   ← 先于 dup check！
构建 position dict（algo_sl_id=None / tp fields / metadata+reasons merge）
_load() → dup check：
    symbol 已有 → '[开仓] 已在持仓中' → return True（order+SL 已孤）
_save(positions) → log → True
open 全程：无 TG、无 PG event（PMB-28）
```

### Full Close（`_close` force=False / force=True）
```
force=False → _was_closed_recently guard → False（PMB-4）
_mark_closed（marker-first，任何 exchange 调用之前）
sandbox → sb_close → record('reason', final_close=True) → pop → save → True
real:
  fapi_get positionRisk(#1, symbol 过滤 side 方向)
  [flat: real_pos 缺] → _cancel_all_algo（异常吞错）→ pnl —
    → record(final_close=True) → PG EXCHANGE_POSITION_FLAT → pop → save → True
  [有仓] → round(abs(positionAmt)) → ExecutionService close intent
      result.code → _log_close_error(60s 全局节流) → _clear_closed_marker →
      False（不 cancel algo —— 裸奔保护）
      result 正常 → fapi_get positionRisk(#2 剩余读)
        remaining >= 0.001 且 reported < 0.001 →
            pos.qty=remaining 回写 → save → return False（无 REC/PG；
            marker 保留 —— PMB-27）
        remaining >= 0.001（部分成交）→ save 先行（PMB-28 序）→
            record(final_close=False) → PG CLOSE_ORDER_PARTIAL →
            pos.qty=remaining 回写 → False；无 cancel；marker 保留
        remaining < 0.001（flat）→ close_qty = filled or requested 兜底 →
            cancel_all_algo（异常吞错）→ pnl → PG CLOSE_ORDER_FILLED →
            log → record(final_close=True) → pop → save → True
  try 域任何异常 → '[平仓异常]' → _clear_closed_marker → False
```

### Partial Close（`_partial_close`）
```
ExecutionService partial intent（SHORT→BUY 反向，无 reduceOnly —— E-OBS-5）
result 非 dict / code → '[分层止盈失败]' → return（state 未改）
exception → log → return
pos['qty'] = round(qty - close_qty, 4)     ← 负数 close_qty 增仓（E-OBS-7）
pnl 仅 log（无 ledger —— OBS-8/PMB-6；无 cancel；无 marker）
_save(positions) → log
```

## 测试文件 → 冻结行为

| 文件 | 测试数 | 冻结范围 |
|---|---|---|
| `test_lifecycle_ordering_golden.py` | 6 | open/close/flat/partial 全链精确排序 |
| `test_lifecycle_failure_golden.py` | 10 | 失败矩阵（open reject/save 静默、close code/异常/部分满、partial 非 dict/code/异常） |
| `test_lifecycle_callers_golden.py` | 8 | 返回契约 / 幂等 / dust / noop cooldown / monitor 调用面 |
| （复用既有）`test_open_golden` `test_close_golden` `test_edge_cases_golden` | 41 | open/close/partial 结果级（Phase 1 起） |

既有观察锁定：OBS-3（dup order-first）、OBS-4/8、E-OBS-5/7/13、PMB-6/10/13。

## Side-effect Matrix（当前真实）

| Path | Binance | PM State | Redis save | Marker | record_trade | PG | cancel algo | Return |
|---|---|---|---|---|---|---|---|---|
| open 成功 | leverage+margin+order+enqueue | 建 | ✓ | 无 | 无 | 无 | — | True |
| duplicate open | leverage+margin+**order+enqueue** | 不变 | 无 | 无 | 无 | 无 | — | True |
| open order reject | leverage+margin (+cancel 无效) | 不建 | 无 | 无 | 无 | 无 | — | False |
| open recent guard | 无 | 不变 | 无 | check | 无 | 无 | — | False |
| open save 失败 | 全部已发生 | 内存有 | ✗（吞错） | 无 | 无 | 无 | — | True |
| full close 成功 | risk×2 + market | pop | ✓（末） | mark First | ✓ final | FILLED | ✓（执行后） | True |
| close exec code fail | risk ×1 | 保留 | 无 | mark→**clear** | 无 | 无 | ✗ | False |
| close 异常 | 视异常点 | 保留 | 无 | mark→clear | 视点 | 视点 | 视点 | False |
| close 剩余无成交 | risk ×2 + market | qty 回写 | ✓ | mark 保留 | 无 | 无 | ✗ | False |
| close 部分成交 | 同上 | qty 回写 | ✓（先于 REC） | mark 保留 | ✓ final=False | PARTIAL | ✗ | False |
| exchange-flat close | risk ×1 | pop | ✓ | mark First | ✓ final | FLAT | ✓ | True |
| sandbox close | 无 | pop | ✓ | mark First | ✓ final | 无 | 无 | True |
| partial 成功 | market（无 reduceOnly） | qty 增/减 | ✓（执行后） | 无 | 无 | 无 | ✗ | None |
| negative partial | market 反向单 | **qty 增** | ✓ | 无 | 无 | 无 | ✗ | None |
| partial failure | risk 未达（视点） | 保留 | 无 | 无 | 无 | 无 | ✗ | None |
| duplicate close (f=False) | 无 | 保留 | 无 | check only | 无 | 无 | — | False |

## 非事务性声明

当前**不是**事务系统：无 rollback / compensation。任何一步失败后：
前序 Binance side effect 已发生、记录可能部分写入、marker 状态可能
中间化——这些全部冻结为 Observed。

## Closed-marker 交互表

| Path | Check | Mark | Clear |
|---|---|---|---|
| normal full close | (force) | mark-first | 无 |
| exchange 拒/异常 | force 或 non-force | mark | clear |
| exchange flat close | (force) | mark | 无 |
| partial | 无 | 无 | 无 |
| duplicate close（recent） | check→skip | 无 | 无 |
| sandbox close | (force) | mark | 无 |

## 新观察（PMB-27 ~ PMB-30，详见 PM_GOLDEN_OBSERVATIONS.md）

- **PMB-27** partial/no-fill close：marker **保留**（成功进入剩余仓路径
  不 clear）——后续轮 force=False 会被 recent-guard skip
- **PMB-28** persistence 顺序不对称：full-close save 在 record/PG **后**；
  partial-fill close save 在 record/PG **前**
- **PMB-29** `_set_cooldown` 为 noop（pass）；冷却由策略主循环负责
- **PMB-30** AlgoSL enqueue 在 dup check 之前 → duplicate open 产生孤
  立条件单（order + 无人认领 SL）

## P7-07B 迁移约束

Lifecycle body 迁出时：**上表每一格的时序（尤其 PMI-23 家族的
marker-first、save 与 REC/PG 的相对序）必须逐位不变**；
return contract 逐路径不变；上游调用面
（monitor `_close` / `close_position` / WS / SE 全部 legacy wrapper
保持签名）。
