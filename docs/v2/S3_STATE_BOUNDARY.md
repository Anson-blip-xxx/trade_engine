# S3 State Boundary（Phase 5-03）

> 基于 feature/v2-architecture @ `0e1bfe4`。目标：把模块级隐藏状态显式化为
> 可注入边界，**同时保持 process-local / restart=清空 / 多实例独立** 的
> Golden 行为。零行为变化。

---

## 1. 模块级可变状态完整盘点（真实代码 @ 0e1bfe4）

| 状态名 | 类型 | key/value | writers | readers | 时间依赖 | 事件相关 | 特征相关 | IO 相关 | P5-03 处理 |
|--------|------|-----------|---------|---------|----------|-------|--------|---------|-----------|
| `_event_states` | dict | `SYMBOL_TYPE` → `{state,strength,ts,sent_ts}` | `_update_event_state`/`_end_expired_events` | 同左 | now（调用方传入） | ✓ | — | — | **pilot 1：注入** |
| `_fb_state` | dict | `symbol` → `{state,last_high,last_low,breakout_*,ts}` | `_detect_failed_breakout` | 同 | `time.time()` 内部 | ✓ | — | — | **pilot 2：注入 + time_fn** |
| `_ema_cache` | dict | `symbol` → `{period: value}` | `compute_ema` 增量路径 | 同左 | 无 | — | ✓ 特征 | — | **不动**（P5-02 纯核已分离；增量路径数值与 full 不同——S3-1 延伸，注入会改数值） |
| `_symbol_klines`/`_current_kline` | dict+lock | `symbol` → kline list | WS/REST/KlineManager | `_merged_klines`/snapshot | t 字段 | 数据 | 数据 | IO | 不动（F 类，Phase 7） |
| `_symbol_windows`/`_symbol_windows_raw` | dict | 快照 | compute_and_detect | 输出层 | ts | — | — | IO | 不动 |
| `_big_orders` | list+lock | 大单 dict | `_on_trade_msg` | analyze 闭包 | ts | 边缘 | — | IO | 不动（D 类） |
| `_API_LAST_CALL`/`_last_heartbeat`/`_ws_kline_connected` | 标量 | — | IO 循环 | 同左 | time.time | — | — | IO | 不动（E/F 类） |

分类依据 §三：A(Event lifecycle)=`_event_states`；B(FAILED_BREAKOUT)=`_fb_state`；
C(EMA cache)=`_ema_cache`；D(cooldown/dedup=内嵌 A 的同状态对象)；E/F 不动。

## 2. 状态技术决策

### dict-protocol 边界（非 God Store、不造 CRUD）

`s3/state.py`（~80 行，stdlib only：`__future__+typing`）：

| 类 | 协议（= dict duck-type） | backing |
|-----|--------------------------|---------|
| `EventStateStore` | `get(key, default)` 返回**引用** / `__setitem__` / `pop` / `__delitem__` / `items()`（底层迭代视图=插入序） / `__len__` | 可包装 legacy dict |
| `BreakoutStateStore` | 同协议 + `get(sym, default)` 返回 default **原对象**（caller 原地改直接进入写入路径，镜像原 `_fb_state.get(sym, default)`） | 同 |

**为什么不是 named-method port**：legacy 函数以 `store.get(key)/store[key]=/
store.pop/del store[k]/store.items()` 原生 dict 语义工作（含 `state['state']='END'`
的原地突变，`del` 在遍历后批量）。命名 API 会迫使 adapter 复制/包裹——反而
破坏引用/迭代序语义；保持 dict duck-type 是行为最忠实的边界。

## 3. Lifecycle pilot（pilot 1）

生产 diff（仅 3 个函数的注入参数，**默认路径逐字等价**）：

```python
def _update_event_state(evt, now, state_store=None):
    store = _event_states if state_store is None else state_store
    # 其余逐字不变（prev=store.get(key); store[key]=...; evt 原地突变）

def _end_expired_events(all_symbols, now, state_store=None): ...
    # iter 序 store.items()（插序）→ 收集 expired → 循环后 del store[k]
    # （先收集后删除的时机逐字保持

def _detect_failed_breakout(..., state_store=None, time_fn=None):
    now = time.time() if time_fn is None else time_fn()
```

### parity 冻结（`test_state_boundary.py`）
- 同一事件序列（ACTIVE→cool skip→delta 放行→END）：legacy vs 注入
  **输出一致 / state 一致 / 时间戳一致**
- END 迭代序：插入序输出（非 strength/sort）
- aliasing：store.get 返回引用，`state['state']='END'` 原地变异 + 键删除
- 时间边界：29s skip / 30s allow / delta 19 skip / 20 放行（注入路径）
- 多实例：两个 store 同输入 → 双发 ACTIVE（S3-4 Golden 不变）

## 4. Breakout pilot（pilot 2）

- BreakoutStateStore 注入 parity：IDLE→BREAKING_HIGH→(确认失败 emit)→IDLE 与
  legacy 完全一致；legacy 与注入最终态一致
- **S3-3 冻结不变**：注入态下真实 window 链（无 close_pos）→ `breakout_confirmed`
  仍恒 False（`test_s33_unreachable_real_chain` 专有测试）

## 5. EMA cache（未接线 — bounded 范围）

- P5-02 已剥离纯内核 `s3.core.ema`；增量路径+cache 在 legacy 模块中完整保留
- **红灯依据**：增量路径返回值与全量路径不一致（P5-02 parity 冻结的事实）；
- **接线需要改变数值语义** → NO-WIRING 正确结果

## 6. 不在本阶段

`_symbol_kline`/`_current_kline`/`_symbol_windows*`/`_big_orders`（F类 IO 态，Phase 7）/$_API_LAST_CALL/$_last_heartbeat/lock 语义/`_ema_cache` 注入。

---

## Golden Observations（S3-8）

### S3-8 — `_update_event_state` 对 evt 原地突变 + 返回同一引用
**Observed**：`evt['state']/['since']` 直接写入传入 dict，返回对象=
同一 `evt` 引用（非拷贝）——`compute_and_detect` 依赖该行为收集
managed events。
**锁定测试**：`test_state_boundary.py::TestLifecycleParity::test_series_identical`
（legacy/injected 注入相同行为）
**Migration constraint**：P5-04 抽 detector 时该 aliasing 语义必须保留；
"返回新对象"= 行为变化。
