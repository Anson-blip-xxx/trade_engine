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

---

## PMB-1 · open 路径的"PM 更新"实现位于 shared_executor，且 pm:positions 双写方并存

### Current behavior:
open 路径的 position 登记由 `shared_executor._update_pos_cache` 完成（写
`_POS_CACHE` + **直写** `pm:positions`），不在 PM 模块内。`pm:positions` 因此有
两个写方：se 直写与 `PM._save`。Redis 异常在两处均被内部静默吞掉。
### Why it matters:
open 路径的 PM 注册失败的 cancel 分支（E-OBS-1）只有当 `_update_pos_cache`
在 Redis try 块**之外**抛异常时才可达（如 `entry:.12g` 格式化失败）；Redis 故障
会被吞掉，open 继续按成功处理。
### Migration constraint:
PositionManagerPort 的 wiring 必须以 `_update_pos_cache` 为唯一实现锚点；
Phase 7 统一双写方前不得改变 `pm:positions` 写入结构或吞错语义。

## PMB-2 · `_POS_CACHE` 写直通是端口实现契约的一部分

### Current behavior:
`_update_pos_cache` 除写 Redis 外还同步更新进程内 `_POS_CACHE[symbol]`，
供 `get_position_count`/`has_position` 快路径使用；`_POS_CACHE` 是模块级
可变全局。
### Why it matters:
未来若把注册移入 PM adapter 而丢失缓存写直通，防重复开仓读路径
（se `_get_positions`）会读到滞后状态，改变 duplicate check 行为。
### Migration constraint:
Port 实现必须保持"缓存+Redis"双写语义（或显式决策放弃），否则触发
E-OBS-2 相关测试回归。

## PMB-3 · close/partial 的 execution result 处理无跨模块缝

### Current behavior:
close 结果处理（`has_remaining_position`/`accounted_close_qty`/`pos['qty']`
更新/pop/save/final_close 标志）全部内联在 `PM._close`/`_partial_close` 内；
不存在类似 open 路径 `_update_pos_cache` 的跨模块 seam。
### Why it matters:
D1 只为 open 路径定义了 `PositionManagerPort`；close 侧无既有 seam 可契约化，
强行发明 "on_close_result" 接口属于接口虚构而非边界提取。
### Migration constraint:
Phase 7 拆解 `_close` 时先把 result-handling 段显式化为 PM 内部函数，
再决定是否暴露 port；本阶段禁止预建接口。

## PMB-4 · `closed:{symbol}` 无 Redis TTL，"4h"由 ts 比较实现

### Current behavior:
`_mark_closed` 写 `{'ts': time.time()}` **不带 TTL**（`redis_store.set` 无
expire 参数）；"4h"窗口语义在 `_was_closed_recently` 中以
`time.time() - ts < 4*3600` 实现。标记若未被 `_clear_closed_marker` 清除
（复开仓位场景），key 将**永久留存**并持续参与窗口判断。
### Why it matters:
代码注释"Redis + 4h TTL"与实现不符（无 TTL）；Phase 7 若把窗口改为真 TTL，
失败恢复语义会变化（4h 后旧标记复活失效 vs 永存）。
### Migration constraint:
PositionStatePort/后续 boundary 不得为 closed marker 引入真实 TTL；
窗口语义必须以 ts 比较实现。

## PMB-5 · Position State 接线 pilot 已落地于 `_save`，读路径与 RMW 未接

### Current behavior:
`PM._save` 经 `PositionStatePort`（`RedisPositionStateAdapter`，镜像
`_load_meta/_save` 语义，注入 `_rget/_rset` 晚绑定）；`PM._load_meta` 与
`se._update_pos_cache`（读-合并-写）仍直连 `_rget/_rset`。
### Why it matters:
双写方（PMB-1）中只有一个写路径穿 boundary；读路径与 RMW 保持现状才能
保证三层加载与 duplicate check 行为零变化。
### Migration constraint:
Phase 7 统一双写方前不得把 `_load_meta`/`_update_pos_cache` 改走
不同实现；接线顺序必须 save → load → RMW 逐个 pilot。

## PMB-6 · partial close 不产生任何台账记录

### Current behavior:
`PM._partial_close` 的成功路径：Binance order → qty 更新 + Redis save，仅此而已。
无 `_pg_record_event`（trade_events）、无 `record_trade`（trade_episodes）、
无 TG、无 algo cancel；PnL 仅存在于 `_pmlog` 日志行。
### Why it matters:
事件级台账（trade_events）缺少 PARTIAL 独立事件，episode 级 PnL 依赖
final_close 时 trade_recorder 的 Redis partial 合并兜底；台账分析
（trade_analyzer/report_performance）对逐段止盈不可见。
### Migration constraint:
D3 依法不补录；若 Phase 7 决定补 ledger 事件，属行为变更需显式决策，
且不得改变现有 record/pg 顺序（E-OBS-6a）。

## PMB-7 · 同一平仓存在两套 PnL 口径（事件级 vs episode 级）

### Current behavior:
`CLOSE_ORDER_FILLED` 事件 `realized_pnl` = PM._close 即时公式
（(entry-price)*qty，core.position_pnl 同值）；随后 `record_trade` 产出
trade_episodes 的 `pnl_usdt` = Binance/REALIZED_PNL income 对账（非零时覆盖）
+ Redis partial 分段合并值。同一次平仓两个 PG 写携带不同 PnL 数字成为合法状态。
### Why it matters:
台账消费方（report/analysis）两处读值不同；事件级与 episode 级无法互检。
### Migration constraint:
PnL 计算不进 PositionLedgerPort（persist already-produced data only）；
口径统一属行为变更，Phase 7 显式决策。

## PMB-8 · Algo cancel 覆盖矩阵（按平仓分支）

### Current behavior:
cancel 调用仅出现在四处场景：① full close 成交后（order 成功 → record前）；
② exchange-already-flat 分支（record 前）；③ `_algo_place_sl_inner` 换单前清理；
④ be_done/trail 换单。**订单被交易所拒绝（rejected）分支不 cancel**（注释明确：
避免删除原止损单导致仓位裸奔）；partial close 分支不 cancel；ghost/WS 清理路径不 cancel。
### Why it matters:
rejected 保留旧 SL 是保护性设计；partial/ghost 不 cancel 可能残留条件单——
均为当前有意/现状行为，改动任何一处都会改变保护窗口语义。
### Migration constraint:
Phase 7 拆解时 cancel 时机必须逐分支冻结（P4-01 spy 序列测试已是契约）；
不得为"统一"在各分支补 cancel。

## PMB-9 · `_algo_cancel` 在 `_s6api` 兜底模式下发 GET 而非 DELETE

### Current behavior:
`_s6api` 兜底元组第 3 槽（fapi_delete 槽位）实为 `_light_fapi_get`。
因此 `_algo_cancel(algo_id)` 在兜底模式下对 `/fapi/v1/algoOrder` 发出 **GET**
请求（应为 DELETE），取消静默失败（light 版吞错、返回 dict/None），旧条件单
可能残留；be_done/trail 换单时旧单残留 + 新单入队 = 多余条件单。
当 binance_api 可导入时第 3 槽为真 DELETE（行为正确）。
### Why it matters:
同一函数两种模式语义实质不同（N2 家族的新实例）；兜底模式下 be_done/trail
的"取消旧单"维度失效。
### Migration constraint:
D4 契约 `cancel_algo_id` 逐字镜像该行为（不改）；修复需把 light 槽位换成
真 DELETE 实现——属行为变更，Phase 7 显式决策。

## PMB-12 · record_trade 内嵌 income 对账量（P7-03A 冻结）

### Observed:
`record_trade` 内嵌 Binance `/fapi/v1/income`（`REALIZED_PNL` 类型）覆盖
公式 PnL——非零 income 以百分比反算到 `pnl_pct=income/notional*100`。
API 失败/空/畸形 → 吞错 → 公式值。**同一平仓事件产生两套 pnl 口径**
（事件级=pm 公式；episode 级=income 对账+partial 加权合并）。
### Migration constraint:
P7-03B LedgerService 必须保留双口径并证明 income/公式在 fail/zero/malformed
三种状态下与 legacy 完全一致；不得统一口径。

## PMB-13 · partial close 无 PG/CH ledger 全链缺失
### Observed:
`final_close=False` 时 record_trade 只积累 Redis partial key；不写
trade_episodes、不写 trade_history。partial close 分支本身也不发 PG 事件。
### Migration constraint:
PMB-6 扩展（ledger 链全段缺失）；P7-03B 不得"顺手补全"。

## PMB-14 · `_algo_place_sl_inner` 内嵌 exchangeInfo（原始 Binance REST）
### Observed:
`_algo_place_sl_inner` 第一段落就直接使用 `requests.get`（非 shared.binance_api）
获取 `/fapi/v1/exchangeInfo` —— LOT_SIZE stepSize 和 PRICE_FILTER tickSize 用来
rounding qty / stopPrice。放入 ProtectionService 时必须经 callable 注入或保留
inline（除非作为 action ticket）。
### Migration constraint:
hidden IO；P7-04B 期间经 callable 注入但不更换 Binance client。

## PMB-15 · Protection `cancel_all_algo` 字杠杆（仅 status ∈ {NEW, WORKING, TRIGGERED}）
### Observed:
`_cancel_all_algo` 只活跃的 algoId ∈ (NEW/WORKING/TRIGGERED) — 其他（EXPIRED/FINISHED）
**跳过不触发 DELETE**。
### Migration constraint:
P7-04B 保留过滤逻辑（不改 status set）。

## PMB-16 · monitor_all 心跳 60s 节流与持仓快照合并（P7-05A 冻结）
### Observed:
- 空仓（`_load()` == {}）：60s 内重复调用完全静默；>60s 才 log `[监控心跳] 无持仓`。
- 持仓：心跳行最多列 8 个 symbol（`list(positions.items())[:8]`），get_price
  成功输出 `{sym}({首字母} pnl:+.1f%)`，失败/无价输出 `{sym}({首字母})`——
  get_price 逐币异常吞错（不中断 sweep）。
### Migration constraint:
节流写回 `_monitor_heartbeat_ts`（模块级全局）；P7-05B 拆分时保留。

## PMB-17 · _RECENTLY_GHOSTED len<6 → side=None → filter 失效被消费（P7-05A 冻结）
### Observed:
ghost 队列多元组若不足 6 项（无 side），`g_side=None` → `not g_side` →
`closed.append(g)` 直接消费——system_filter 完全失效，S6 进程可能记账 S8 的
AlgoSL ghost 平仓。
### Migration constraint:
P7-05B 保留意图不加"修复"；若作为 action ticket 必须显式记录。

## PMB-18 · time_extended 后 deadline 失效（延期只生效一轮，P7-05A 冻结）
### Observed:
`time_extended=True` 后：deadline 检查跳过（`if not time_extended` 包住了
deadline 判断），下一轮 hold>ts_min 直接平『时间止损』。延期语义 =
"再观察一次周期"，非"延长到 deadline"。
### Migration constraint:
P7-05B 保留（意图不重建为 deadline 语义）。

## PMB-19 · _save 保存全量 all_positions（filter 只影响 sweep 范围，P7-05A 冻结）
### Observed:
monitor_all(system_filter) 只把 _monitor_one 应用到过滤后的 positions；
但 `all_positions`（不过滤）被终局 `_save` 整量写回——meta_filtered 兜底
回写 + 终局快照 = 每轮两次整量 save。
### Migration constraint:
双 save（_load meta_filtered + monitor_all 终局）保持；P7-05B 不合并。

## PMB-20 · WS lease fail-open（redis 异常 → 允许连接，P7-05A 冻结）
### Observed:
`_ws_am_leader` 包 try/except：任何 redis 异常 → `return True`——锁服务
失效时双进程可能同时连 listenKey（原本要防的互相踢线在异常时回归）。
### Migration constraint:
保留 fail-open 意图（选择可用性优先）。

## PMB-21 · WS 平仓 → 先记账后标记（P7-05A 冻结）
### Observed:
`_ws_on_message` 删仓时：先 `_try_record_ghost_trade`（此时 marker 未设，
自身拦截不生效）→ 成功后 `_mark_closed`。顺序用于跨进程去重：对方进程
`_was_closed_recently` 命中即可跳过。
### Migration constraint:
record→mark 顺序保持。

## PMB-22 · 1h 反转分支 return None 阻断当轮时间止损（P7-05A 冻结）
### Observed:
`_monitor_one` 第 6 步：SHORT ema9>ema20*1.02（或 LONG ema9<ema20*0.98）
命中后即使未平仓也 `return None`——当轮时间止损不再评估（下一轮才可能平）。
### Migration constraint:
P7-05B 保留链序（勿把 return None 改为 fall-through）。
