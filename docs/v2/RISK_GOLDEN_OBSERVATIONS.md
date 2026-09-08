# Risk Golden Observations（P3-03/P3-04）

> 记录 Risk characterization 期间观察到的当前行为。
> 全部已锁定（tests/risk/），后续重构时显式决策是否保留。

---

## RISK-1 · min_notional 可以覆盖 risk cap

- **Behavior**: `AtrRiskPositionSizer.budget()` 最后一步 `max(position_usdt, min_notional)`
  可能**覆盖** risk cap 的缩减结果。
- **Current implementation**: `position_models.py` L23-25 → 先 risk cap → 后 min_notional floor。
- **锁定测试**: `test_sizer_budget_min_notional_floor` / `test_small_balance_floor`
- **Potential concern**: 极端小余额时 min_notional 10 兜底可能导致实际风险 > 1% 上限。
- **Future phase**: Phase 7（是否加 min_notional 上限校验需显式决策）。

## RISK-2 · PUMP_* leverage 恒为 2

- **Behavior**: `leverage_for_score` 中 PUMP_UP/PUMP_DOWN base=2，即使 score=100/atr=0
  也不升杠杆（其他 event_type 在同等条件下可到 5 或 3）。
- **Current implementation**: `risk/core.py` leverage_for_score base 表。
- **锁定测试**: `test_leverage_pump_always_2`
- **Potential concern**: PUMP 类策略可能被系统性压制杠杆。
- **Future phase**: Phase 7（如果是有意设计则保留，如果是遗漏则调整 base）。

## RISK-3 · calc_position_qty 双套池化计算

- **Behavior**: `calc_position_qty` 中 `remaining * alloc_pct` 与
  `sizer.budget()` 内部的 `min(balance * pool_budget, remaining) * score_fraction`
  存在公式重复——两套池化计算并存，结果一致但维护时容易只改一处。
- **Current implementation**: `shared_executor.py` calc_position_qty + position_models.py budget。
- **锁定测试**: `test_calc_qty_normal` / `test_sizer_budget_risk_cap`
- **Future phase**: Phase 7（合并为单一计算路径）。

## RISK-4 · malformed peak 导致 AttributeError

- **Behavior**: `_drawdown_status` 读 Redis `account:peak` 时，若值不是 dict
  （如 str 'garbage'），`peak.get('bal', 0)` 直接抛 AttributeError（无 try/except）。
- **Current implementation**: `risk/service.py` drawdown_status。
- **锁定测试**: `test_malformed_peak_string_raises`（pytest.raises(AttributeError)）
- **Potential concern**: Redis 故障/数据损坏时 _drawdown_status 会崩，导致主循环异常。
- **Future phase**: Phase 7（加 defensive 处理需显式决策）。

## RISK-5 · drawdown loss_lock 重试后仍返回 halt

- **Behavior**: loss_lock 后 6h 重试时，代码重置 loss_lock=False 并返回 0.0
  （仍 halt）。下次调用会重新走 4h 等待窗口，实际等待时间为 6h+4h=10h。
- **Current implementation**: `risk/service.py` drawdown_status loss_lock 分支。
- **锁定测试**: `test_loss_lock_after_6h_resets_to_halt`
- **Potential concern**: 恢复周期可能比预期的 6h 更长。
- **Future phase**: Phase 7（如果需要调整 recovery 周期需显式决策）。
