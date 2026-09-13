# S0 Phase 6 Closure（P6-05 · Phase 6 验收）

> 基于 feature/v2-architecture @ `6b59b2a`。Production diff = **0**
> （P6-02/03/04 只做等价委托）。回答：S0 是否可演进？

---

## 1. 最终架构（真实状态，诚实标注）

```text
S3 market:s3_data / market:sentiment / Binance ticker
       ↓
S0MarketDataPort ← S0RedisMarketAdapter        【P6-04 input boundary】
       ↓
Legacy sampling shell（保留）
       ├── sample_btc（BTC → ema/atr/amp）
       ├── sample_breadth（top50 pool + strict close>ema20>0）
       ├── sample_sentiment
       ├── sample_shock_score / sample_alts_sync（wall-clock mod gate，S0-3）
       └── BreadthPoolMemoryState（6h cache，process-local）
       ↓
s0.core.classify_regime                        【P6-02 pure classifier】
       ↓
compute_state legacy wrapper（version/timestamp 注入）
       ↓
S0PublisherPort ← S0LegacyPublisherAdapter     【P6-03 三写链】
       ├── Redis market:s0（latest slot，无 TTL）
       ├── 原子文件 market_state.json（tmp+rename，out-raise）
       └── ClickHouse default.market_state_log（warning-no-raise）
       ↓
Consumers：
       ├── shared_executor.market_allows_trading（读 market_mode —— S0-1 fail-open）
       ├── s0_reader（S7）：version/stale gate + fallback
       ├── shared/market_data
       └── trade_recorder
```

诚实标注：
- main loop（60s cadence）仍在 legacy shell（未拆）
- fapi_get 仍为 S0 自带实现（与 shared.binance_api 平行，未统一）
- wall-clock gate 仍在 wrapper（classifier 纯）
- producer 无 stale check（consumer 才有）
- **S0-1 mismatch 未修**（fail-open 现状冻结）

## 2. Pure Core
`classify_regime(...)` 全部 7 行决策表（P6-00 §13 全覆盖）+ 矛盾区
bull+normal+0.32→weak_bull / breadth='weak'→weak_bear 双路径（tie-break 顺序
冻结）+ sentiment 地板 -7；stdlib-only imports `{__future__, typing}`。

## 3. State Boundary
`BreadthPoolMemoryState` —— 唯一 process-local state（6h pool cache）；
classifier 无状态；`_breadth_symbols_*` 的 global 赋值经 port put 桥接。

## 4. Classifier Boundary
`classify_regime` 逐字保留：strict `< > >= <=`（严禁改写成 max/min）；
contradictory zone 冻结双路径。

## 5. Publisher Boundary
`S0PublisherPort.publish_state(state)`——单一 ordered chain；adapter 经
callable 注入三写支点；`S0.Redis→file→CH` 失败语义逐分支镜像（S0-9）。

## 6. Remaining IO
`fapi_get`（legacy S0 客户端）、merchant `sample_shock_score` / `sample_alts_sync`、
sentiment 真数据来源（sentiment_bridge 端由 PORT 读取）、Critical metrics 统计。

## 7. Failure Matrix（全部 OBSERVED 不修）

| Failure point | 当前行为 |
|---------------|----------|
| S3 Redis read 失败 | sample_* 默认值 → classifier 依附**
| malformed S3 snapshot | data 无 'symbols' 键 → {} 空快照 |
| ticker fetch 失败 | get_breadth_symbols 吞错 → 沿用旧池 |
| top50 refresh 失败 | 同上（S0-4 fallback 链） |
| old pool missing | pool=[] → breadth 0.5normal（S0-4） |
| sentiment read 失败 | {} → 缺省 neutral risk=False |
| classifier exception | sample 处外层 catch → 60s 后再试（ survives） |
| Redis publish write 失败 | 吞错 → file/CH 照常 |
| file write 失败 | **raise** → CH 跳过 |
| CH write 失败 | warning-no-raise → 视为成功 |
| stale state | consumer 侧 180s gate（S7）；producer 无 |
| multi-instance overwrite | 后写覆盖 + CH 双行（S0-8） |

## 8. Known Frozen Behaviors（全部 KNOWN/FROZEN）
S0-1..S0-10 + own-fapi_get 双实现 + producer 无 stale check + 无 leader
coordination + wall-clock mod 门 + EMA/S3/S6 内部风格差异 —— 索引见
PHASE6_GOLDEN_BEHAVIOR_INDEX.md。

## 9. Multi-instance semantics
S0-8 保持：无 leader/协调/lock，last-writer-wins；CH 可重复行。

## 10. Replay determinism limitations
Wall-clock mod 门（S0-3）产出与调用时刻相关：相同输入可能产生不同
shock/alts；S0 无 np hysteresis。

## 11. Phase 6 dependency
后续 S6/S8/Decision/PM（Phase 7+）可通过 s0_reader 继续消费；
s0.core 可作为 Regime Engine 完整纯内核被下游复用。

## 12. Phase 7 dependency
- 把 S0 与 S6/S8 消费端（S0-1 mismatch）以统一字段（market_state 或改名）
  一起解决——迁移后显式行为变更 ticket
- publisher 三写的 CH 批量/连接池优化属于 Phase 7 DB 层
- breadth pool 与 fapi_get 并入 shared Binance client（Phase 7）
