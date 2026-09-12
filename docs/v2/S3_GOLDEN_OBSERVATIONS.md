# S3 Golden Observations（P5-01）

> 冻结当前行为；不改、不修。每条有对应锁定测试。

### S3-1 — `event:s3` 是 latest-slot 覆盖，非队列
**Observed**: production每周期（60s）全量覆盖 `event:s3`，无事件时也覆盖写入 `{ts, events: []}`；无队列/pubsub 锁，Redis helper 文件降级 key 同名。
**锁定测试**: `test_compute_and_detect.py::test_second_cycle_overwrites_first`、`test_redis_semantics`（内嵌于 compute/failure 文件）
**Migration constraint**: P5-02/04 抽 Publisher 时保留"整量覆盖+空也写"；不得改成 push 队列。

### S3-2 — 429 retry 当前递归且无明确上限
**Observed**: `_api_get` 收到 429 → log + `time.sleep(5)` + **递归自调用**；连续 429 会持续递归（有限 Python 深度内完成），无 max-retry 计数。
**锁定测试**: `test_failure_behavior.py::test_api_get_global_rate_limit_state`（调用计数冻结）；429 分支静态事实见 docstring
**Migration constraint**: 抽 IO adapter 时保留递归 path 或显式写入行为变更 ticket；不得"顺手修"。

### S3-3 — `close_pos` 从未被 producer 写入 → `_strong_breakout` 恒 False → `breakout_confirmed` 恒 False
**Observed**: `compute_window_data` 不产 `close_pos`；`detect_events._strong_breakout` 读 `w15m.get('close_pos', 50)` → 默认 50 命中（>=65 / <=35 均不满足）；超买/超卖 guard 的 breakout 解封路径因此从未激活。
**锁定测试**: `test_detect_events.py::TestPulseGuards`（真实链 delay + 单元注入 close_pos 唤醒路径双向验证）
**Migration constraint**: P5-02 抽 Feature Core 时冻结"producer 不写 close_pos"的缺省行为；补写属行为变更需显式 ticket。

### S3-4 — 冷却/去重/状态机全部进程内存；多实例可重复事件
**Observed**: `_event_states`(30s+delta20)/`_fb_state`(7.5h 状态机)/`_ema_cache` 均无持久化与跨实例协调——两个独立进程处理同一市场快照会**各自**发出 ACTIVE 事件（无分布式 dedup）。
**锁定测试**: `test_lifecycle_state.py::TestMultiInstanceDedup::test_inmemory_dedup_is_process_local`
**Migration constraint**: State 脱离后（P5-03）必须保留 process-local 语义；若迁 Redis 属行为变更。

### S3-5 — 事件级无独立 timestamp
**Observed**: 只有快照级 `ts`；事件带有 `since`（生命周期模块态时间）与 END 才有 `duration`；per-event ts 缺失 → 由消费方（read_s3_events）依赖 `snapshot.ts + 90s` stale gate。
**锁定测试**: `test_detect_events.py::TestPulse::test_pulse_up_trigger_and_strength`、`test_s3_integration_golden.py::TestUnifiedSignalCompatibility::test_s6_signal_fields`
**Migration constraint**: Unified Signal `timestamp=None` for S3；补 per-event ts 属行为变更。

### S3-6 — module-level 生命周期状态重启即失效
**Observed**: `run()` 只从 Redis 恢复 klines（`cache:s3_rolling`）——`_event_states/_fb_state/_ema_cache` 直接为空：重启后现行高/低冷却清零、FAILED_BREAKOUT 状态机归零，可能出现重启后事件重复发布或"伪新事件"。
**锁定测试**: `test_failure_behavior.py`（`_ema_cache` 增量分支）+ inventory 部分 `/ D5 fixture reset model`
**Migration constraint**: P5-03 引入 StatePort 时调用方须保持"重启重置"语义（除非显式变更）。

### S3-7 — P5-01 期间所得的强度修正事实
**Observed**: HIGH_VOL 最低强度并非 0——阈值 `vol_ratio>=2` 使 `int(2.0*15)=30` 成为实际下限；`LOW_VOL` 同道（>=0.3 且 >0 区间内 min=10）。"0 强度可达"在当前阈值下不可达。
**锁定测试**: `test_detect_events.py::TestVolRegime::test_high_vol_boundary_strength`
**Migration constraint**: P5-02 拆 Score/Classifier 时按此修正设计假设。
