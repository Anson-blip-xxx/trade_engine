# Phase 6 Golden Behavior Index（P6-05）

> 索引：ID / Area / 一句话行为 / 锁定测试 / 未来处理阶段。
> 原始细节：S0_INVENTORY.md / S0_GOLDEN_OBSERVATIONS.md / PHASE6_CLOSURE 文档。

| ID | Area | Behavior | Test file | Future handling |
|----|------|----------|-----------|-----------------|
| S0-1 | Consumer contract | market_state/market_mode mismatch → risk-off 门失效 fail-open | test_s0_consumers.py / test_phase6_consumers.py | 显式行为变更 ticket（迁移后） |
| S0-2 | Consumer default | is_system_allowed 缺键 → True（"保守默认"注释失实） | test_s0_consumers.py | 行为变更 ticket |
| S0-3 | Wall-clock | alts_sync/shock_score 受 %1800/%60 mod 门；一半时间恒 0（determinism 限制）；core 本身 deterministic | test_compute_state.py / test_phase6_integration_closure.py | 显式策略 ticket |
| S0-4 | Breadth 缺省 | 空/断流 universe → ratio=0.5='normal'假象 | test_breadth.py | 行为变更 ticket |
| S0-5 | Breadth 稀释 | S3 缺失 symbol 计入 denominator 不计 numerator → 数据缺使 ratio 低漂 | test_breadth.py / test_breadth_pool_state.py / integration | 行为变更 ticket |
| S0-6 | Regime 分层 | 无 SOFT/HARD——单布尔 risk_off + 5 档 regime | test_compute_state.py | 策略变更 ticket |
| S0-7 | 状态机 | 无 hysteresis/previous-regime——全量重算（stateless classifier） | test_compute_state.py | 策略变更 ticket |
| S0-8 | 多实例 | 无协调 last-writer-wins；CH 双行 | test_s0_consumers.py | Phase 7 |
| S0-9 | Publisher 失败语义 | Redis 吞错/file raise/CH warning-no-raise 三态分明 | test_s0_consumers.py / test_publisher_boundary.py | Publisher 改造 ticket |
| S0-10 | 阈值严格性 | strong/normal/weak 全为 strict 不等号（0.70→normal 非 strong） | test_breadth.py | 保留 |
| P6-02 | Classifier core | s0.core stdlib-only + 全分类规则 | test_regime_core.py / architecture | — |
| P6-02/A | 决策表 | 7 行全 freeze + bull+normal+0.32 → weak_bull 矛盾区 + breadth='weak' → weak_bear 分支 | test_compute_state.py / test_phase6_integration_closure.py | — |
| P6-03 | 写序 | Redis → atom file → CH 对拍 + CH row shape 逐字 | test_publisher_boundary.py | — |
| P6-04 | 输入 | S3 snapshot 缺失/畸形语义；BTC 全字段；ticker 端点冻结；6h 严格边界 | test_input_boundary.py / test_breadth_pool_state.py | — |
