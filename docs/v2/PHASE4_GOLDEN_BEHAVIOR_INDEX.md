# Phase 4 Golden Behavior Index（P4-03-01-D5）

> 仅供索引：ID / 归域 / 行为一句话 / 锁定测试 / 后续处理阶段。
> 原始细节在各 ID 所在文档；**一律 OBSERVED/FROZEN，不是修复清单**。

## Execution（E-OBS，P4-01/02/03）

| ID | Area | Behavior | Test file(s) | Future phase |
|-----|------|----------|--------------|--------------|
| E-OBS-1 | Open order params | SE：无 positionSide/reduceOnly + RESULT；PM：+BOTH | test_open_execution.py / test_execution_core.py | Phase 7 |
| E-OBS-1a | Open failure | 单次尝试无 retry；无 PM/PG/TG/Algo | test_open_execution.py / test_phase4_integration_closure.py | 显式决策 ticket |
| E-OBS-2 | Open→PM 时序 | duplicate/existing 检查在 order 前（gate），PM 后置 | test_phase4_integration_closure.py | Phase 7 |
| E-OBS-3 | Algo SL | 异步 11s 队列；成交→无保护窗口；无 retry | test_algo_sl.py / test_protection_boundary.py | ticket |
| E-OBS-4 | 测试基建 | 直接改模块属性跨测污染 → 必须 monkeypatch | test_execution_core.py | — |
| E-OBS-5 | Close/partial 非对称 | close reduceOnly='true'；partial 无 | test_close_execution.py / integration closure | ticket |
| E-OBS-6 / 6a | Close 时序 | marker 先行；flat 分支 record 在 pg 前 | test_close_service_integration.py / closure | Phase 7 |
| E-OBS-7 | Partial 负 qty | qty 放大（10-(-100)=110），无 clamp | test_execution_core.py / closure | ticket |
| E-OBS-8 | Partial 副作用 | 无 record/PG/TG/algo | closure（PMB-6） | ticket |
| E-OBS-10 | 四套 Binance 实现 | 沙盘拦截/错误语义不同（N2/N3） | test_phase4_architecture.py | Phase 7 |
| E-OBS-11a | Sandbox 拦截 | path 含 'order' ✓含 algoOrder；注入共存零变化 | test_round_sandbox.py | Phase 7 |
| E-OBS-13（新） | Open TG 失败 | tg_fn 异常→False 但订单/PM/PG 已成 | test_phase4_integration_closure.py | ticket |

## PM（P1-04：OBS-1..10）

OBS-1 position_id 双格式 / OBS-2 signal_type 字段名 / OBS-3 开仓后查重 /
OBS-4 零/负 close_qty / OBS-5 _clear_closed_marker 直连 / OBS-6 SANDBOX `_close_position`
键比 / OBS-7 qty=0 merge / OBS-8 partial 无 / OBS-9 marker 先行（→E-OBS-6） /
OBS-10 持久化静默 —— 详见 PM_GOLDEN_OBSERVATIONS.md，锁定 tests/position_manager/（74）；Phase 7。

## PMB（D1-D4 期间）

| PMB | Behavior | Test file | Future |
|-----|----------|-----------|--------|
| 1 | open 注册在 se + 双写方 + cancel 分支可达性 | test_pm_boundary.py | Phase 7 |
| 2 | `_POS_CACHE` 写直通契约 | 同上 | Phase 7 |
| 3 | close/partial result 无跨模块缝 | 同上 | Phase 7 |
| 4 | closed marker 无 TTL（4h=ts 比较） | test_position_state_boundary.py | ticket |
| 5 | wiring pilot 现状（_save only） | 同 | Phase 7 |
| 6 | partial 无 ledger | closure | ticket |
| 7 | 双 PnL 口径（event 公式 vs income 合并） | closure/index 文本 | ticket |
| 8 | cancel 覆盖矩阵（rejected 保留 SL） | closure/index | Phase 7 |
| 9 | `_algo_cancel` 兜底 GET 冒充 DELETE | test_protection_boundary.py | ticket |

## D5 新增锁定（test_phase4_integration_closure.py）

- Open 跨边界序列（service→pm_update→pg）；TG 失败 → False 但已注册（E-OBS-13）
- Close full/flat 全序；rejected 无 cancel；exception 清 marker；remaining 分支选择
- Partial：PG/ledger/mark/Algo 零写入 + 负 qty 原样
- Protection：payload/写回/失败无写回/cancel 异常不阻断/enqueue 失败开仓继续 True
