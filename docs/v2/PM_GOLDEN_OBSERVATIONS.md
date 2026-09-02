# PM Golden Tests — Observed Current Behavior 记录（P1-04）

> 本文档记录 Golden Tests 冻结的 Position Manager 当前行为中**疑似问题/不一致**的部分。
> P1-04 一律不修复，只在后续阶段有意识地处理。
> 每条都有对应的锁定测试，重构时若测试失败 = 行为被改变，必须显式决策。

---

## OBS-1 · PM 内两种 position_id 格式并存

- **Behavior**: `_merge_meta` 内联生成三段式 ID `system:symbol:open_time(.6f)`；
  `_position_id()` 生成四段式 `system:symbol:entry(.12g):open_time(.6f)`（含 entry）。
- **Current implementation**: `position_manager.py` `_merge_meta`（内联 f-string）与 `_position_id()`。
- **锁定测试**: `test_merge_position_id_format_frozen` / `test_position_id_format_frozen`
- **Potential concern**: 同一持仓在不同代码路径可能得到不同 ID，影响台账关联（PG
  `trade_episodes.position_id` 的稳定性）。
- **Future phase**: Phase 7 PM decomposition（统一 ID 生成器）。

## OBS-2 · open_position 与 _close 的信号字段名不一致

- **Behavior**: `PM.open_position` 写入字段 `signal_type`；`_close` 记账时读取
  `pos.get('event_type')`。经 `PM.open_position` 直开的仓位，recorded
  signal_type **恒为空串**。
- **Current implementation**: `position_manager.py` open_position（写 signal_type）
  / `_close`（读 event_type）。
- **锁定测试**: `test_sandbox_close_long_records_and_removes`（断言 recorded
  signal_type == ''）。
- **Potential concern**: 经 PM 直开的仓位丢失信号归因。生产未暴露是因为
  S6/S8 经 shared_executor 写入的字段名恰好是 `event_type`。
- **Future phase**: Phase 7（统一字段名，或两个 open 路径合一）。

## OBS-3 · 同 symbol 重复 open：订单已发出但记录不覆盖

- **Behavior**: `open_position` 先发市价单，之后才发现 symbol 已在
  `pm:positions` → 只打日志、**返回 True**、不覆盖记录。
- **Current implementation**: `position_manager.py` open_position（先下单后查重）。
- **锁定测试**: `test_open_same_symbol_order_placed_but_record_not_overwritten`
- **Potential concern**: 交易所实际仓位与 PM 记录不一致（双倍敞口）；
  返回 True 会误导调用方"新仓位已登记"。
- **Future phase**: Phase 7（查重应前置到下单之前）。

## OBS-4 · _partial_close 缺少 close_qty 有效性校验

- **Behavior**: `close_qty=0` 仍会发出 qty=0 的订单（真实交易所会拒绝）；
  `close_qty<0` 时 `pos['qty'] = qty - (-x)` 会**增加**持仓数量。
- **Current implementation**: `_partial_close`（无参数校验）。
- **锁定测试**: `test_partial_close_zero_qty_freezes_current_behavior` /
  `test_partial_close_negative_qty_increases_qty`
- **Potential concern**: 上游传参错误时会产生反向成交/无效请求。
- **Future phase**: Phase 7（加参数校验属行为变更，需显式决策）。

## OBS-5 · _clear_closed_marker 绕过 Redis 注入缝

- **Behavior**: PM 其余 Redis 访问走 `_rget/_rset`（可注入），但
  `_clear_closed_marker` 惰性 `from shared.redis_store import delete`
  **直连真实 Redis**。
- **Current implementation**: `position_manager.py` `_clear_closed_marker`。
- **锁定测试**: `test_closed_marker_lifecycle`（fixture 必须额外 patch
  `shared.redis_store.delete` 才能隔离）。
- **Potential concern**: 隐藏依赖；测试/沙盘环境下清除的是真 Redis 的 key。
- **Future phase**: Phase 7（统一经注入的 Redis 访问）。

## OBS-6 · 沙盘平仓路径不日志化 pnl

- **Behavior**: `_close` 沙盘路径计算 pnl_pct/pnl_u 后既不日志也不传递
  （pnl 只在实盘路径以 `pnl=+x.x% (+y.yyU)` 形式日志化）；record_trade
  参数中也不含 pnl（由 trade_recorder 自行从 entry/price/qty 推导）。
- **Current implementation**: `_close` 沙盘分支。
- **锁定测试**: `test_sandbox_close_*`（无 pnl 日志断言）。
- **Potential concern**: 沙盘回放时 pnl 只能从参数推导，无法直接对账。
- **Future phase**: Phase 7。

## OBS-7 · _merge_meta 允许 qty=0 的持仓进入合并结果

- **Behavior**: 交易所快照 qty=0 时，合并输出仍包含该持仓（qty=0）；
  qty>0.001 的过滤在上游 REST 层完成，不在 `_merge_meta`。
- **Current implementation**: `_merge_meta`。
- **锁定测试**: `test_merge_meta_zero_exchange_qty_enters_merged`
- **Potential concern**: 下游若直接消费 `_merge_meta` 输出会见到零仓位。
- **Future phase**: Phase 7。

## OBS-8 · _partial_close 不产生 record_trade

- **Behavior**: 分层止盈只更新 qty 并落盘，PnL 仅写日志；交易台账在
  最终全平时由 `record_trade` 聚合产出（final_close 语义）。
- **Current implementation**: `_partial_close`（无 record_trade 调用）。
- **锁定测试**: `test_partial_close_long_pnl_direction_and_remaining`
- **Potential concern**: 若进程在部分平仓后崩溃，已实现部分的 PnL 无台账。
- **Future phase**: Phase 7。

## OBS-9 · _close 先标记后平仓

- **Behavior**: `_close` 在尝试平仓**之前**就 `_mark_closed`（防重入）；
  平仓失败时再 `_clear_closed_marker` 恢复。两步之间存在其他进程跳过
  该 symbol 平仓的窗口。
- **Current implementation**: `_close` 开头。
- **锁定测试**: `test_close_post_rejected_clears_marker_returns_false`
- **Potential concern**: 多进程场景下失败窗口内该 symbol 失去平仓保护。
- **Future phase**: Phase 7。

## OBS-10 · 持久化失败静默吞掉

- **Behavior**: `_save` / `_load_meta` / `_mark_closed` 等对 Redis 异常全部
  `except: pass`（或返回空 dict），调用方无感知。
- **Current implementation**: `_save` / `_load_meta` / `_mark_closed`。
- **锁定测试**: `test_save_swallows_redis_errors` /
  `test_load_meta_redis_error_returns_empty`
- **Potential concern**: Redis 故障期间持仓变更可能只存在于内存/丢失，
  且无任何告警。
- **Future phase**: Phase 7（fail-open 保留，但应加告警）。
