# PM State Golden Index（P7-01）— StateService P7-02 拆分约束

> 映射 Golden ID → state area → test file → P7-02 必须保持的约束。
> 原始细节：PM_GOLDEN_OBSERVATIONS.md / PM_PHASE7_PLAN.md。

| Golden ID | State area | Test file | P7-02 强制约束 |
|-----------|-----------|-----------|----------------|
| OBS-1 | position_id 双格式 | test_persistence_golden.py | 保留 seed（dual format 不统一） |
| OBS-5 | `_clear_closed_marker` 直连 Redis | test_closed_marker_golden.py | 直连 delete 语义保留 |
| OBS-7 | qty=0 merge | test_merge_golden.py | merge 逐字 |
| OBS-10 | 静默吞错 | test_s0_consumers.py（跨套件复用） / test_state_failure_golden.py | 异常类不统一 |
| PMB-1 | 双写方 | test_state_rmw_golden.py::TestMultipleWriters / _update_pos_cache | last-writer-wins 顺序可写 |
| PMB-2 | _POS_CACHE 写直通 | test_state_rmw_golden.py / test_s0_consumers | 写直通不得丢失 |
| PMB-4 | marker 无 TTL（ts 比较） | test_closed_marker_golden.py::TestClosedMarker | ts 比较口径不动；вид tx no TTL |
| PMB-8 | (via S0-4) empty pool 默认 0.5 normal | test_breadth.py | 分母空值不转 error |
| PMB-9 | fallback GET（—） | （tests/execution 冻结） | 不修 |
| PMB-10 | marker 边界 4h（ts 比较而非 TTL） | test_closed_marker_golden.py::TestClosedMarker::test_ts_age_four_hours_frozen | 4h ts 比较语义 |
| PMB-11 | _load_meta 每次构造 fresh dict（就地修改不回写；aliasing 只在 inner ref） | test_state_rmw_golden.py::TestStateAlias | fresh-dict-per-call 语义保留 |
| load-chain | 三层 WS→REST→meta；ws_fresh 30s 严格 `now-ts<30` | test_state_loading_golden.py | 层序与回退直写 |
| REST 层 | 最新 positionRisk → merge → _save | test_state_loading_golden.py::TestWSFallback | REST 失败 → 静默向 meta 收敛 |
| categorize | `_was_closed_recently` 过滤层 | test_state_loading_golden.py::test_meta_layer_filtered | marker 过滤保持 |
| RMW | partial close qty rmw | test_state_rmw_golden.py::TestRMW | round(qty,4) 无 validate（PMB-11） |
| POS_CACHE | entry/side/type | test_state_rmw_golden.py::TestMultipleWriters | unreachable legacy field shapes 冻结 |
| positive/dual writers | last-writer-wins | test_state_rmw_golden.py::TestMultipleWriters | 两个写者都可写 |
| topology | Redis get/set/marker delete failures | test_state_failure_golden.py |Not unified |
| OBS-3 | 同 sym 重复 open 冻结 | test_state_rmw_golden.py::TestMultipleWriters::test_open_position_dup_record | 冻结 |
| restart | globals reset → redis 服务 | test_state_rmw_golden.py::TestRestart | 重启只需 redis |
