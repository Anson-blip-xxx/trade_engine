# Phase 5 Golden Behavior Index（P5-05）

> 索引：ID / Area / 一句话行为 / 锁定测试 / 未来允许修改阶段。
> 原始细节：S3_INVENTORY.md / S3_GOLDEN_OBSERVATIONS.md / S3_STATE_BOUNDARY.md。

| ID | Area | Behavior | Test file | Future phase |
|----|------|----------|-----------|--------------|
| S3-1 | event:s3 语义 | latest-slot 全量覆盖（空也写，非 queue） | test_compute_publish_integration.py | Publisher 改造需显式 ticket |
| S3-2 | _api_get 429 | 递归重试无上限 | test_failure_behavior.py | 显式修复 ticket |
| S3-3 | breakout 不可达 | producer 无 close_pos → breakout_confirmed 恒 False | test_detect_events.py / detector / state versions | 策略变更 ticket |
| S3-4 | dedup 是进程内存 | 双实例同输入双发 ACTIVE | test_lifecycle_state.py / state_boundary | 分布式 dedup=行为变更 |
| S3-5 |事件级无 ts | 只快照 ts；adapter timestamp=None | test_s3_integration_golden.py | 行为变更 ticket |
| S3-6 | 重启丢生命周期 | 仅 klines 经 cache:s3_rolling 恢复 | test_failure_behavior.py | Phase 7（S3 decomposition） |
| S3-7 | HIGH_VOL 强度 | 阈值使实际下限=30（0 不可达） | test_detect_events.py | — |
| S3-8 | evt 原地突变 | _update_event_state 返回同引用（detector→lifecycle identity） | test_state_boundary.py / test_detector_core.py | P6 前必须保持 |
| S3-9 | 源码序 vs 写层序 | detector append 按源码；输出层 sort(-strength) | test_detect_events.py / test_compute_publish_integration.py | Publisher 改造需冻结 |
| S3-10 | VIOLENT 独立于 breakout | 方向由 mid 比较定（与 close_pos 无关），真实可达 | test_detector_core.py | — |
| S3-11 | 单 try publish 拓扑 | set 失败 → publish 不执行；publish 失败 → market 仍写 | test_compute_publish_integration.py | 显式 ticket |
| EMA | 双路径差异 | full ≠ incremental（values[0] 语义） | test_feature_core.py / failure | 显式修复 ticket |
| latest-slot | Redis | 同 S3-1 | 同 | — |
| multi-instance | 部署形态 | 重复事件/覆盖 cache:s3_rolling | inventory 文本 | Phase 7 |
| hidden file logging | _log | 文件写（吞错） | test_failure_behavior.py | 显式 ticket |
| WS reconnect | kline/trade WS | run_forever + 指数退避（未完全 characterization） | inventory 文本 | P6 |
