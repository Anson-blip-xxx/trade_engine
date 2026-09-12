# S3 Phase 5 Closure（P5-05 · 验收）

> 基于 feature/v2-architecture @ `9fcd58e..`，production diff = s3_orderflow.py
> 输出段 22 行 → 4 行 thin delegation（其余零改动）。
> 回答："S3 是否已从 God Module 变成可理解、可测试、可演进的四件套？"

```text
Market Input / Snapshot   ← IO（fetch/WS/_api_get 保留 God Module 现址）
        ↓
s3.core                   ← Feature Core（P5-02，纯）
        ↓
s3.detector               ← Event Detector（P5-04，pure-ish；breakout_runner 注入）
        ↓
s3.orderflow lifecycle    ← ACTIVE/UPDATE/END（P5-03 可注入口保留现址）
        ↓
s3.ports.S3RedisPublisher ← Event Snapshot / Market Snapshot 写 + publish（P5-05 拆）
        ↓
Redis helpers（shared.redis_store，未改）
```

## 1. Pure Core
`s3/core.py`：ema/rsi/atr/build_window_features——非 textbook 标准（RSI 简版、
EMA 增量路径 values[0] 语义）**未标准化**——保持并冻结。

## 2. State Boundary
`s3/state.py`：EventStateStore/BreakoutStateStore（dict-protocol 精确镜像）；
生产 default=legacy `_event_states/_fb_state`；`_ema_cache` NO-WIRING（增量
路径返回值与全量不一致，接线会改数值）；`_symbol_klines/_big_orders/locks`
不在边界（Phase 7）。

## 3. Detector Boundary
`s3/detector.py`：detect_candidate_events——13 family 源码序 if 链逐字；
log_fn/breakout_runner 注入；THRESHOLDS 唯一原型。

## 4. Publisher Boundary
`s3/ports.py`：S3RedisPublisher（注入式 redis_set/redis_publish）：
- `write_event_snapshot(events, ts)`：latest-slot + S3-9 sort + 单 try publish
  拓扑（S3-11）+ 空事件不 publish
- `write_market_snapshot(symbols, ts)`：独立 try
- 不含 rolling-cache / signal:s3_signals / mover:s3_spot —— 均在线程闭包
  （KlineManager.save_cache / ws_big_order_loop），本阶段仅 contract +
  测试冻结（不动线程代码）。
- 接线：compute_and_detect 输出段 22 行 → 4 行（factory per call =
  晚绑定 `_rset/_rpublish`，保留 monkeypatch 语义）。
- 接口未含 save_rolling_cache 等三族（不入 port——离线程闭包太近，风险高，
  Phase 7 一起）。

## 5. Remaining IO（未迁移）
fetch_klines / get_top_symbols / _api_get(限速+429 recursion S3-2) / spot
momentum / WS 线程 / KlineManager / market_brain_loop 编排 / 信号与大单写。

## 6. Failure Matrix（11 项 — 全 OBSERVED 不修）

| Failure point | Current behavior |
|---------------|------------------|
| feature compute error | 上层 try 吞错 → 60s 后重试 |
| detect error | 上抛至 market_brain_loop 外层 → 吞错+60s（test：传播出 compute_and_detect） |
| lifecycle error | 同——上层 catch |
| event:s3 写失败 | 吞错；非空时 publish 不执行（S3-11） |
| market:s3_data 写失败 | 吞错；event 快照已写则保持 |
| cache:s3_rolling 写失败 | 吞错（KlineManager.load/save 内部） |
| publish 失败 | 吞错；market 仍写（S3-11） |
| spot momentum 失败 | 空 dict（requests except 吞） |
| API 429 | sleep5 + 递归重试（S3-2 KNOWN FROZEN） |
| WS stale | kline loop reconnect 1→30s 指数退避；REST 回退 age>180s |
| malformed kline | 静默 pass（WS 消息级） |

## 7. Known Frozen Behaviors（Phase 5 不修）
S3-1..S3-11 + EMA 双路径 + multi-instance 重复事件 + hidden file logging +
WS reconnect 未全 characterization —— 全部 KNOWN/FROZEN（索引见
PHASE5_GOLDEN_BEHAVIOR_INDEX.md）。

## 8. Multi-instance semantics
进程内存态重启=清空、dedup=本地 → 双实例双发（S3-4 维持原状；分布式去重=
行为变更，需显式 ticket）。

## 9. Replay determinism limitations
`_event_states/_fb_state/_ema_cache` 进程内存态 → backtest/replay 无法
确定性重放 FAILED_BREAKOUT/EMA 增量路径；event:s3 只保留 last snapshot =
缺失 per-event 原子性（万一并发读取，读方各窗口 race 可见性由 90s stale 兜底）。

## 10. Phase 6 dependency
S6/S8 主循环消费 S3 snapshot 的稳定性（90s stale gate）依赖 event:s3 与
publish 节奏（每 60s）——Phase 6 S6/S8 重构不得改变 snapshot shape
（`{ts, events[]}`）与 `s3:event:notify` 频率契约。

## 11. Phase 7 dependency
EventDataStream（WS/REST fetch）、KlineManager、编排（market_brain_loop）→
Phase 7 S3 decomposition（本 closure 完成纯四层+IO 让位）。
