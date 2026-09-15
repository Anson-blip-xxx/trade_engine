# Phase 8-00 — Architecture Cleanup Inventory（docs/tests only；production 0 diff）

> 基于 feature/v2-architecture @ `dc08647`。Phase 8 启动盘点：把 Phase 7 关闭时
> 残留在 PositionManager / Service 包中的 items 分类为
> **A. 安全 Cleanup（无行为变更）** 与 **B. Behavior-change ticket（行为变更需显式决策）**，
> 给出 dependency map 与风险分级。**本阶段生产零改动**——只产出 backlog 与分类。

## 一、当前产物形态（事实）

- `shared/position_manager.py`：**1296 行 / 67 函数**（P7-08 收口态）
  - 组成：26 个 compatibility wrappers（thin delegate）、6 个晚绑定 factory +
    execution factory、12 组 runtime global backing、
    三层 `load` 链/merge glue、algo 队列 + worker、`_light_*` 轻量 IO helpers、
    `_set_cooldown` noop、SYSTEM_CFG/_SYSTEM_KEYS 配置。
- `position_state/ledger/protection/monitoring/reconcile/lifecycle service.py`：
  独立 ownership（122/346/64/425/304/383 行），全部 late-bound 注入、零反向 import
  （P7-08 AST seal 39 tests 守护）。
- `strategies/shared_executor.py`：1102 行；`from shared.position_manager import
  monitor_all, close_position, _algo_enqueue, _algo_start_worker, _load`——
  唯一直接 import PM 的非测试模块（import 时启动 algo worker = N1 冻结）。
- 间接消费者：services/s0、s3_orderflow 不 import PM（独立进程态）。

## 二、Dependency Map（当前真实）

```text
strategies/shared_executor.py ──import──▶ shared/position_manager.py（facade）
shared/position_manager.py ──delegate──▶ {state, ledger, protection,
                                          monitoring, reconcile,
                                          lifecycle, execution} services
lifecycle ──ci/pi intents──▶ execution/core（注入） ；exec 几乎 ──▶ execution/service
monitoring ──gcl──▶ reconcile（info ghost 也经 it 注入）；reconcile ──▶ (无 sibling)
全部 service ──callable 注入──▶ PM 全局 runtime backing（唯一 owner）
```

禁止方向验证保持：`service → PM / sibling` = 0（closure 审计 PASS）。

## 三、Cleanup Backlog — A 类（安全，无行为变更）

| # | Item | 现状 | 建议动作 | 依赖面 / 断点 |
|---|---|---|---|---|
| C1 | `_light_fapi_*`/`_light_get_price` 与 ExecutionService 双轨 IO | PM 仍持 legacy 直连 | 收敛到 execution Port 或封装为共享 helper（行为逐字镜像） | PM 内私有调用（无外部 import） |
| C2 | `_ensure_apikey/_API_KEY/_API_SECRET` 内联惰性 apikey 逻辑 | **P8-04 完成**：GREEN parse 机械迁 `position_market/auth.py`（load_api_keys/build_api_header/has_credentials）；`is None` call-time 懒加载 + globals 缓存语义 == HEAD；YELLOW hmac/signature/timestamp/recvWindow 留原位（Red，不迁） | **position_market/auth.py** | C1 `_light_fapi_*` 0 diff（未迁） |
| C3 | 37/41/22 注入位大构造器（lifecycle/monitoring/ledger） | 全 kw 参数 | 分组结构体（StateSeams/IOSeams 等）——**纯重构** | 需全 parity 套件绿 |
| C4 | `_load_meta` / `_save` / `_state_service` / `_position_state` 4 段重复 factory 胶水 | 各自晚绑定 | 合并成一个 state 胶水 helper | `_load` chain + marker |
| C5 | `_get_funding_rate` 残留 requests 直连（fapi premiumIndex，失败→0） | PM inline | 抽 shared/market_data helper | monitoring `fund` seam 引用 |
| C6 | `_algo_place_sl_inner` 内联 exchangeInfo（PMB-14） | frozen IO | 预取缓存/注入 helper | Protection chain |
| C7 | daemon 线程启动 glue | **P8-06B 完成**：thread mechanics 迁 `position_runtime/runtime.py`（algo worker loop/start + WS boot；行为逐字）。**Runtime backing deliberately remains PM-owned**（queue/flag/lock/WS state/heartbeat/`_WS_THREAD` 均不改 owner） | `position_runtime/` | runtime module 仅 thread/start glue，无 RuntimeManager |
| C8 | `SYSTEM_CFG`/`_SYSTEM_KEYS` 常量段 | PM 顶层 | 移 `config/pm_system.py` | monitoring/lifecycle 注入 |
| C9 | `request/context dataclass` | — | Phase 8+（spec 延后项） | 需全量 parity |
| C10 | tests 帮助函数 `pm_full`/`mon` 重复 patch 集 | 测试代码 | conftest 复用 | tests-only |

## 四、Behavior-change Ticket backlog — B 类（不得顺手做）

| # | Ticket | 关联 | 风险等级 |
|---|---|---|---|
| T1 | duplicate open 前置查重（连带后置 PMB-30 孤立 SL 消失/转移） | OBS-3/PMB-30 | **HIGH**（open 行为变更：返回值/侧效应序） |
| T2 | partial close ledger 补齐（含 PG/CH full 链） | PMB-6/13 | 中（台账口径变） |
| T3 | 负数 partial 校验/clamp | E-OBS-7 | 中（qty 口径变） |
| T4 | PMB-9 `fapi_delete` 槽位实为 GET → 真正 DELETE | PMB-9 | 中（algo cancel 生效变） |
| T5 | 11s 无 SL 窗（enqueue→worker 11s） | E-OBS 家族 | 高（风险敞口语义） |
| T6 | ghost 队列 side=None 消费（PMB-17）| PMB-17 | 中（跨系统记账口径） |
| T7 | pop-before-record 修正（先记账后清理 or 失败补偿） | PMB-23 | 中（对账每次失败窗口） |
| T8 | silent reconcile channel 统一 | PMB-24 | 中（通道合并 debate） |
| T9 | dead `_RECENTLY_GHOSTED` runtime | PMB-25 | 低（激活 or 删除） |
| T10 | marker 自愈 / TG→PG 吞错不对称 | PMB-26 | 低 |
| T11 | partial marker/recent-guard 风险 | PMB-27 | 中 |
| T12 | save ordering 对称化 | PMB-28 | 高 |
| T13 | cooldown 真实现 | PMB-29 | 中（并发覆盖风险） |
| T14 | dup-check 前移（依赖 T1） | PMB-30 | 高 |
| T15 | S0-1 `market_state/market_mode` fail-open 修复 | S0-1 | 中 |
| T16 | runtime adoption policy（exchange-only 自动 adopt or notify-only） | — | 中 |
| T17 | Position ID 统一 | OBS-1/2 | 中（PG 关联键变化） |

## 五、Dependency-risk Classification（对 cleanup 的映射）

- **Green（纯清理）**：C2/C3/C4/C8/C9/C10 — 模块边界内搬家/分组，golden 不动
- **Yellow（helper 引用粘合）**：C1/C5/C6 — 需要每处替换后 golden 等值证明（像 D2 pilot）
- **Red( unerwing-B)**：T5/T12/T14 —— 涉及交易所/资金安全语义，必须独立票、独立回滚预案

## 六、Runtime globals 单 owner 现状（保留态）

12 组 backing 全部留在 PM：`_WS_POSITIONS/_WS_LOCK/_WS_LAST_UPDATE/_WS_STOP/
_WS_LEASE_KEY/_WS_LEASE_TTL/_WS_INSTANCE/_RECENTLY_GHOSTED/_ALGO_QUEUE/
_ALGO_QUEUE_LOCK/_ALGO_WORKER_STARTED/_CLOSE_ERROR_LOG_TS`（+
`_last_algo_update/_last_api_call/_S6_API/_big_orders` 内嵌 2 组）。
Phase 8 C16 建议：以单一 `Runtime` dataclass 归拢但**不换 backing 位置**
（identity 不变）。

## 七、Test 盘点现状

- tests/position_manager：436 behavioral golden（P7-01~07A 家族全绿）+ 48 closure guard
- tests/execution：394（Phase 4）；full：1741+1

## 八、P8-00 Exit

1. docs-only ✅ 2. backlog（A/B 分类）✅ 3. dependency map ✅
4. 行为风险分类（Green/Yellow/Red）✅ 5. production 0 diff ✅
