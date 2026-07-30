# Trading Engine 系统拓扑图

## 架构概览

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         systemd 进程管理                                │
│  trade-s0.service  trade-s3.service  trade-s6.service  trade-s8.service  │
│  (市场保护)        (行情分析)         (做多执行)        (做空执行)        │
└─────────────────────────────────────────────────────────────────────────┘
```

## 组件关系图

```
                          ┌───────────┐
                          │  Binance   │
                          │  Futures   │
                          │    API     │
                          └─────┬─────┘
                                │
              ┌─────────────────┼────────────────────┐
              │                 │                     │
              ▼                 ▼                     ▼
      ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐
      │  shared/     │  │  strategies/  │  │  shared/          │
      │  binance_api │  │  S3.py       │  │  position_manager │
      │  .py         │  │  (行情分析)   │  │  .py (PM)         │
      │              │  │              │  │                   │
      │  fapi_get/   │  │  事件:       │  │  开/平/监控 全周期 │
      │  post/delete │  │  s3_events   │  │  Algo SL Worker   │
      └──────┬───────┘  │  .json       │  │  (后台线程)        │
             │          │  行情:       │  └────────┬──────────┘
             │          │  s3_market_  │           │
             │          │  data.json   │           │
             │          └──────────────┘           │
             │                                     │
             ▼                                     ▼
    ┌────────────────┐                  ┌───────────────────┐
    │ shared/        │                  │ shared/           │
    │ market_data.py │                  │ clickhouse_client │
    │ (行情指标计算)  │                  │ .py (CH SDK)     │
    └────────────────┘                  └───────────────────┘

    ┌────────────────────────────────────────────────────────┐
    │                   shared_executor.py                     │
    │  S6/S8 共用层: open_position, pm_monitor, calc_qty,    │
    │  get_position_count, 信号冷却, TG通知, 状态持久化       │
    └────────────────────────────────────────────────────────┘
               ▲                           ▲
               │                           │
          ┌────┴────┐               ┌──────┴──────┐
          │ S6.py   │               │   S8.py     │
          │ (做多)  │               │   (做空)    │
          │         │               │             │
          │ MAX=2   │               │  MAX=2      │
          │ PULSE_UP│               │  PULSE_DOWN │
          │ TREND_UP│               │  TREND_DOWN │
          │ VIOLENT_│               │  VIOLENT_   │
          │ BULLISH │               │  BEARISH    │
          └────┬────┘               └──────┬──────┘
               │                           │
               ▼                           ▼
          ┌──────────────────────────────────────┐
          │          Redis 存储                   │
          │  pm:positions  (PM持仓元数据)          │
          │  state:s6      (S6冷却/状态)           │
          │  state:s8      (S8冷却/状态)           │
          └──────────────────────────────────────┘
```

## 数据流 (一个完整周期)

```
[S3 行情分析]
     │
     ├── s3_events.json      ← PULSE_UP / TREND_UP / VIOLENT_BULLISH 等事件
     └── s3_market_data.json  ← 各币种多周期 K线 / EMA / ATR / RSI
     │
[S6/S8 主循环 每10s]
     │
     ├── 1. pm_monitor()
     │      ├── monitor_all() → _load() (Binance API → 合并pm:positions元数据)
     │      ├── _monitor_one() 逐个检查: 硬止损 → be_done → 追踪 → 时间止损
     │      └── _refresh_positions() → 更新 _POS_CACHE
     │
     ├── 2. 处理事件 (for evt in events)
     │      ├── is_event_fresh()  ← 信号180s新鲜度检查
     │      ├── get_position_count() ← _POS_CACHE + _PENDING_OPENINGS
     │      ├── cooldown 检查  ← state:s6/s8
     │      ├── market_allows_trading() ← market guard
     │      ├── has_position() 重复持仓检查
     │      ├── 趋势/波动率过滤
     │      ├── calc_position_qty()
     │      └── open_position()
     │             ├── fapi_post MARKET 订单 (Binance)
     │             ├── _update_pos_cache() → _POS_CACHE + _PENDING_OPENINGS
     │             ├── TG 通知
     │             └── _algo_enqueue() → Algo SL Worker
     │
     └── 3. 心跳日志: get_position_count()
```

## 进程边界 & 数据持久化

```
┌─────────────────────────────────────────────────────────────────┐
│  进程 S6 (python3 strategies/S6.py)                              │
│  ┌──────────────────────────────┐                               │
│  │ 内存中:                      │                               │
│  │  _POS_CACHE       ← _load() │                               │
│  │  _PENDING_OPENINGS          │                               │
│  │  _ALGO_QUEUE                │                               │
│  │  _ghost_seen     (函数属性)  │                               │
│  └──────────────────────────────┘                               │
│  Redis: pm:positions  (读写)                                     │
│  Redis: state:s6      (读写)                                     │
│  文件: logs/s6/       (日志)                                     │
│  文件: closed_markers/ (关闭标记, 跨进程去重)                     │
│  TG: 开仓/平仓通知                                                │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│  进程 S8 (python3 strategies/S8.py)                              │
│  ┌──────────────────────────────┐                               │
│  │ 内存中:                      │                               │
│  │  _POS_CACHE       ← _load() │                               │
│  │  _PENDING_OPENINGS          │                               │
│  │  _ALGO_QUEUE                │                               │
│  │  _ghost_seen     (函数属性)  │                               │
│  └──────────────────────────────┘                               │
│  Redis: pm:positions  (读写)                                     │
│  Redis: state:s8      (读写)                                     │
│  文件: logs/s8/       (日志)                                     │
│  文件: closed_markers/ (关闭标记, 跨进程去重)                     │
│  TG: 开仓/平仓通知                                                │
└─────────────────────────────────────────────────────────────────┘

⚠ 关键: S6 和 S8 是独立进程，共享 Redis/pm:positions，但各自有独立
   _POS_CACHE/_PENDING_OPENINGS。pm:positions 由 _load() 写入/读取。
```

## 文件依赖链

```
S6.py ───────────────────────────────────────────────────────┐
S8.py ───────────────────────────────────────────────────────┤
    │                                                         │
    └──→ strategies/shared_executor.py                        │
           │                                                   │
           ├── shared/position_manager.py (PM)                │
           │      ├── shared/binance_api.py (轻量API)          │
           │      │      └── config/binance.env (密钥)         │
           │      ├── shared/redis_store.py                   │
           │      ├── shared/clickhouse_client.py (CH SDK)    │
           │      ├── scripts/sandbox.py (沙盘, 可选)          │
           │      └── strategies/shared_executor.py (s6api)   │
           │                                                   │
           ├── shared/market_data.py                          │
           │      └── shared/binance_api.py                   │
           │                                                   │
           └── shared/trade_recorder.py                       │
                  └── shared/clickhouse_client.py              │
                                                                │
config/binance.env ← 密钥/TG 配置 (S6/S8/PM 共用)               │
s3_events.json    ← S3 产出, S6/S8 消费                          │
s3_market_data.json ← S3 产出, S6/S8 消费                       │
```

## 核心函数索引 (修改影响范围)

| 函数 | 所在文件 | 行号 | 调用方 | 修改风险 |
|------|----------|------|--------|----------|
| `open_position` | `shared_executor.py` | ~378 | S6, S8 | **高** — 开仓全流程 |
| `get_position_count` | `shared_executor.py` | ~313 | S6, S8 | **高** — 仓位上限 |
| `_update_pos_cache` | `shared_executor.py` | ~273 | `open_position` | **高** — 计数原子性 |
| `pm_monitor` | `shared_executor.py` | ~316 | S6, S8 | **高** — 每轮循环入口 |
| `_load` | `position_manager.py` | ~392 | PM, shared_executor | **高** — 数据源合并 |
| `_monitor_one` | `position_manager.py` | ~701 | `monitor_all` | **中** — 止损逻辑 |
| `_close` | `position_manager.py` | ~1142 | `_monitor_one` | **高** — 平仓 |
| `_algo_enqueue` | `position_manager.py` | ~199 | `open_position`, PM | **中** — 条件单 |
| `_refresh_positions` | `shared_executor.py` | ~258 | 各策略 | **高** — 缓存刷新 |
| `calc_position_qty` | `shared_executor.py` | ~378 | S6, S8 | **低** — 数量计算 |

## Redis Key 清单

| Key | 写入者 | 读取者 | 说明 |
|-----|--------|--------|------|
| `pm:positions` | `_load` (PM) | PM, S6/S8 via `_load` | 持仓元数据+合并数据 |
| `state:s6` | S6 | S6 | 冷却期, 状态 |
| `state:s8` | S8 | S8 | 冷却期, 状态 |

## 日志体系

| 日志路径 | 写入者 | 轮转 |
|----------|--------|------|
| `logs/s6/YYYYMMDD.log` | S6 | logrotate 每日, 保留30天 |
| `logs/s8/YYYYMMDD.log` | S8 | logrotate 每日, 保留30天 |
| `logs/s0/YYYYMMDD.log` | S0 (Market Guard) | logrotate 每日 |
| `logs/s3/YYYYMMDD.log` | S3 (行情) | logrotate 每日 |
| `logs/position_manager/YYYYMMDD.log` | PM | logrotate 每日 |
| `logs/position_manager/closed_markers/` | PM | 手动清理 |

## 典型故障排查路径

```
问题: "一直开仓, 仓位上限失效"
  1. 查 S6/S8 心跳 → 当前持仓: N
  2. 查 get_position_count() 返回值 ← _POS_CACHE + _PENDING_OPENINGS
  3. 查 _load() 返回值 ← Binance API + pm:positions 元数据
  4. 查 closed_markers/ ← _was_closed_recently 阻塞真实持仓
  5. 查 _update_pos_cache 是否执行 ← open_position() 成功路径

问题: "TG 重复通知"
  1. 查 S6/S8 日志中 ✅ 开多/开空 次数
  2. 查 get_position_count 在一个循环内是否真实递增
  3. 查 open_position() 中 TG 通知位置 (已改到缓存更新之后)

问题: "持仓不被监控/不平仓"
  1. 查 pm:positions 中该币 sl 值 → 0 表示无止损
  2. 查 _load() 合并结果 → Binance 有仓但 pm:positions 无元数据
  3. 查 _monitor_one() 检查链 → 哪个条件跳过了
```
