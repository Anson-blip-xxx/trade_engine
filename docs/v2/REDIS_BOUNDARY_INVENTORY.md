# Redis Boundary Inventory（Phase 4-03-01-D2 · READ-ONLY 分类 + 最小 Position State boundary）

> 基于 feature/v2-architecture @ `617f2b3` 的实际代码逐行核实。
> 本阶段只建立 **Position State**（`pm:positions`）的最小 boundary；
> 其余 Redis 类别全部保持现状不抽象。

## 0. Redis 基础设施事实（shared/redis_store.py，未改动）

- 唯一 client：`redis.Redis(host=127.0.0.1, port=6379, db=0, decode_responses=True)`
  （模块内全局 `_REDIS`，30s 重试）；**本阶段未创建任何新 client**
- 值格式：JSON 字符串（`json.dumps(indent=2, default=str)`）；
  读取 `json.loads` → dict
- **helper 自身永不向调用方抛错**：get 失败 → 文件降级（KEY_MAP 路径）；
  set 失败 → 日志 + 降级；均内部吞错
- **文件双写**：`set(key, data, *, double_write=True)` 默认同时写 JSON 文件
  （`pm:positions` → `trading_engine/shared/config/pm_state.json`）
- **无 TTL**：`set` 无 expire 参数；所有"窗口"语义（如 4h）由值内 `ts` 比较实现

## 1. 全量 Redis key 分类（A-G）

| Key | 类 | R/W | 生产方 | 消费方 | 值格式 | TTL | 异常行为 | 当前 helper | 现 owner | 进入 D2 boundary |
|-----|---|------|--------|--------|--------|-----|----------|------------|----------|------------------|
| `pm:positions` | **A** | R/W | `se._update_pos_cache`(RMW) / `PM._save` / migrate_all | `PM._load_meta`(→`_load` 三层) / `_load_meta`(meta 层兜底) / `_save` / se `_refresh_positions`/`get_position_count`/`has_position`/`has_any_position` | JSON dict `symbol→position` | 无 | helper 内吞错+文件降级；seam 层 `_load_meta` try→{}、`_save` try→pass | `shared.redis_store.get/set` | PM（se 为开仓路径写方，PMB-1） | **是**（唯一） |
| `closed:{symbol}` | B | R/W/D | `PM._mark_closed` | `_was_closed_recently`(PM+se 副本)/WS 幽灵流 | JSON `{'ts': float}` | **无**（4h 是 ts 比较，非 Redis TTL） | mark/clear 吞错；was 读错→False | `_rget/_rset`；clear **直连** `redis_store.delete`（OBS-5） | PM | 否（Closed Marker 类） |
| `account:peak` / `account:dd_pause` | C | R/W | `risk.service.drawdown_status` | se `_drawdown_status` | JSON | 无 | 注入 redis_get（risk service） | redis_store | Risk | 否 |
| `checkpoint:pnl` / `breaker:circuit` / `state:trader` | C | R/W | s6_auto_trader | s6_auto_trader | JSON | 无 | helper 吞错 | redis_store | s6 legacy | 否 |
| `cd:loss` | D | R/W | trade_recorder | 策略冷却 | JSON dict | 无 | helper 吞错 | redis_store | trade_recorder | 否 |
| `state:s6` / `state:s8` | D/F | R/W | se `save_state` | se `load_state` / PM `migrate_existing_positions`(_SYSTEM_KEYS) | JSON（cooldowns 等） | 无 | helper 吞错 | redis_store | S6/S8 运行时 | 否 |
| `event:s3` / `event:tv` / `signal:s3_signals` / `signal:s2_latest` / `market:sentiment` | E | R/W | S2/S3/sentiment 桥 | se `read_event*` 等 | JSON | 无 | helper 吞错 | redis_store | Signal/S3 | 否 |
| `market:s0` | F | R/W | S0 | se `market_allows_trading` / `get_market_state` | JSON | 无 | helper 吞错 | redis_store | S0 | 否 |
| `market:s3_data` / `cache:s3_rolling` / `mover:s3_spot` / `pool:candidate` / `log:trade` | F | R/W | S3/analyzer | 对应消费方 | JSON | 无 | helper 吞错 | redis_store | 各自模块 | 否 |
| `state:grid`（S7）/ `state:sandbox` / `pm:paused` / `s8b:seen_pumps`（KEY_MAP） | F/G | 各自 | — | — | JSON | 无 | helper | redis_store | S7/sandbox/KEY_MAP 仅登记 | 否 |
| `share:positions` | F/G | R | s6_auto_trader legacy | 同 | JSON | 无 | helper | redis_store | s6 legacy | 否 |
| `event:analysis_reject` / `notify:close` / `alert:external_position:*`（含 pending） | D/G | R/W | se `_record_analysis_decision` / `_should_notify_close` / `PM._notify_external_position` | 面板/去重 | JSON dict | 无 | 吞错（se 侧 try/except） | `_rget/_rset` | se / PM | 否 |
| `pm:monitor:writer` | G | 锁 | se `pm_monitor` | 同 | 字符串 owner | **TTL=30s**（lock ttl 参数） | 失败→False | `lock_acquire/release` | se | 否（coordination lock，非 Position State） |
| `pm:ghost_close:{sym}` | G | lock | `_try_record_ghost_trade`/`_ghost_cleanup` | 同 | 字符串 owner | ttl=60 | 失败→跳过 | lock | PM | 否 |
| `ws:leader` | G | lease | PM `_ws_am_leader` | 同 | 字符串 | ttl=45 | 失败→True 兜底 | lock_renew | PM | 否 |

> 结论：**Position State 仅 `pm:positions` 一个 key**（producer/consumer 如上；
> 每 key 详情记录于本表）。其余 key 不属于本阶段 boundary，全部保持现状。

## 1. 真值模型（Cache vs Source of Truth——不得混淆）

```text
Exchange positionRisk  ←—— 真相来源（唯一事实判断层）
      ↓ 三层加载（PM._load：WS 实时 → REST 轮询 → 本地 meta）
Redis pm:positions    ←—— 本地元数据层（position_id/signal_type/algo_sl_id/
      │                    tp_done 等交易所没有的 enrichment），
      │                  文件双写 fallback（shared/config/pm_state.json）
      ↘ se._POS_CACHE   ←—— 进程内周期缓存（延迟数据，防重复开仓快路径）
PostgreSQL trade_events ←—— 台账（事件流），非当前持仓
```

- `pm:positions` **不是唯一真相**：代码注释写"唯一数据源"意指"PM 内部唯一持仓
  登记位置"，实际语义 = 本地元数据 store + 双进程协调层；真相 = 交易所。
- `_POS_CACHE` 与 Redis 的关系：`_update_pos_cache` 写 Redis **并**同步写缓存
  （write-through）；`_refresh_positions` 反向从 Redis 刷新缓存。两层是
  "store + 进程缓存"，不是"cache vs db"。
- 因此本 boundary 命名为 **PositionStatePort**（state store），
  **不使用** "SourceOfTruth/Repository" 类命名。

## 2. Position State 最小契约（从真实代码导出）

pm:positions 在生产代码中仅有的两个成对 seam（per-symbol 增删接口不存在）：

| Port 方法 | 镜像的生产函数 | 行为 |
|-----------|----------------|------|
| `load_positions() -> dict` | `PM._load_meta` | `_rget('pm:positions')` → 顶层非 dict→{}；值非 dict 过滤；异常→{} |
| `save_positions(positions) -> None` | `PM._save` | `_rset('pm:positions', positions)` 整量快照；异常静默；无 TTL；文件双写原样 |

第三种模式 `se._update_pos_cache` 的 read-modify-write（open 路径）**不在**
本 Port——它是"读-合并-写"编排模式（PMB-1），Phase 7 统一双写方后再定。

## 3. Adapter（execution/adapters/position_state.py）

```text
PositionManager
    ↓ _position_state() 工厂（注入 _rget/_rset，晚绑定）
RedisPositionStateAdapter          （mirror _load_meta/_save 语义）
    ↓
现有 helper（shared.redis_store.get/set → redis client + 文件双写）
```

- 不创建 client / 不改连接池 / 不改序列化 / 不改 key / 无 TTL
- 异常语义 = 镜像（吞错位置与生产 seam 完全一致）
- 依赖方向：PM → execution.adapters（单向），无循环

## 4. Wiring 状态（pilot）

| 路径 | 状态 | 依据 |
|------|------|------|
| `PM._save`（Position State 写路径） | ✅ **已 wiring**（pilot） | 9 条安全条件逐条可证：mechanical delegation / 参数值返回值一致 / exception 镜像 / key 'pm:positions' 逐字 / 无 TTL / 快照语义 / 调用序不变 / monkeypatch 兼容（工厂每次调用晚绑定 `_rget/_rset`）/ 无循环依赖 |
| `PM._load_meta`（读路径） | ❌ 未 wiring（下一 pilot） | 有 No-wiring 状态冻结测试 |
| se `_update_pos_cache` RMW | ❌ 未碰（PMB-1 异常可达性事实，保持） | — |
| `closed:{sym}` / 锁 / 告警等全部其他 key | ❌ 不在 boundary | C-G 类，Phase 7 |

## 5. Golden 行为（保持，不修复）

OBS-5（`_clear_closed_marker` 绕过注入缝）、OBS-10（持久化静默）、PMB-1/2/3
以及 `closed:{symbol}` 无 Redis TTL（4h 为 ts 比较——见 PMB-4）全部保持原样。

## 6. Phase 7 仍需迁移的 PM Redis 职责

`_load_meta` 接线、`se._update_pos_cache` RMW 统一双写方、closed marker 三函数、
`alert:external_position:*`、migrate 读取 `state:s6/s8`、锁类（monitor writer/
ghost/WS leader）——全保留现址。
