# S0 Golden Observations（P6-01）

> producer=services/s0/s0_market_guard.py；consumer=s0_reader / shared_executor /
> shared/market_data；全部 OBSERVED，不修。

### S0-1 — market_state / market_mode 键名失配 → S6/S8 risk-off 门失效（fail-open）
**Observed**: S0 写 `market_state`（"risk-off"/"trend"/"range"）；
`shared_executor.market_allows_trading` 读 `market_mode` → S0 不写该键 →
`mode='normal'` 恒真 → **risk-off 市场下 S6/S8 gate 仍放行**。
机制对照测试证明：写 `market_mode='risk_off'` 时 gate 生效（机制在，键名错）。
**锁定测试**: `test_s0_consumers.py::TestS01FailOpenFrozen`（5 tests）
**Migration constraint**: 修复=行为变更（risk-off 会真实阻断开仓）——须
显式 ticket；过渡期重构（P6-02/03）必须保留该 fail-open 现状。

### S0-2 — is_system_allowed 缺键默认 True（"保守默认"注释失实）
**Observed**: `f"s{N}_allowed"` 缺失/整条 state 缺失/畸形 → 一律 True。
**锁定测试**: `TestS02DefaultAllow`（4 tests）
**Migration constraint**: default 语义改动=行为变更须 ticket。

### S0-3 — alts_sync / shock_score 按墙钟 mod 门填写
**Observed**: `%1800<30` / `%60<30` 决定字段是否采样；其余周期**输出固定 0/默认**——
相同市场输入、不同墙钟时刻输出不同（determinism limitation）。
**锁定测试**: `test_s0_integration_golden.py::TestWallClockMod`（4 tests）
**Migration constraint**: 读取方不得把 0 当真实值判定；边界重构需保留
"不采样→0" 的字段语义。

### S0-4 — 空/失联 universe → breadth_ratio=0.5 → breadth 'normal'
**Observed**: `total=0` 分支 `ratio = above/total if total else 0.5`——
空池断流被折叠成"中性 normal"假象（非保守空头）。
**锁定测试**: `test_breadth.py::TestEmptyPoolFrozen`（3 tests）
**Migration constraint**: 不得改成 error/fast-fail——行为变更。

### S0-5 — S3 数据缺失稀释 breadth（分母计数分子不计）
**Observed**: `sample_breadth` 对 top50 池逐币计数；S3 无该币窗口 →
`total+=1` 但不计 above → 缺数据越多 ratio 越低（向 weak 漂移，非报错）。
**锁定测试**: `test_breadth.py::test_missing_s3_symbols_dilute_denominator_only`
**Migration constraint**: P6-02 拆 classifier 时保持稀释算式。

### S0-6 — risk-off 无 SOFT/HARD 分级
**Observed**: 单布尔 risk_off（三条件 OR：`(btc<ema60且atr_expanding) OR
amp>0.04 OR breadth_ratio<0.30`）+ market_state 三档。
**锁定测试**: `test_compute_state.py::TestRiskOffThresholds`（4 tests）
**Migration constraint**: 新增分级=行为变更 / 需显式策略 ticket。

### S0-7 — 无状态分类器：重启全量重算
**Observed**: 无 previous-regime/hysteresis/transition cooldown；重启后第一
周期即可切档；wall-clock mod 门使采样取决于进程重启时刻。
**锁定测试**: `test_compute_state.py`（decision table 全绿）+ inventory 文本
**Migration constraint**: 增加状态机=显式策略变更。

### S0-8 — 多实例无协调 → last-writer-wins
**Observed**: 无 leader lock/publish；两实例写 market:s0 = 后写覆盖；
ClickHouse market_state_log 双行（事件历史将重复）。
**锁定测试**: `test_s0_consumers.py::TestMultiInstanceLastWriterWins`
**Migration constraint**: 引入 leader=行为变更。

### S0-9 — 写序：Redis → 原子文件 → ClickHouse，三种失败语义各异
**Observed**: ① redis 写失败 → 吞错（文件/CH 照写）；② 文件写失败 → 异常
**上抛**（write_state 无 try 包文件 IO，main 外层 catch）；③ CH 失败 →
warning 不抛（redis+file 已成事实不回滚）。
**锁定测试**: `test_s0_consumers.py::TestWriteStateOrder`（3 tests）+
`test_s0_failure_behavior.py`（2 tests）
**Migration constraint**: P6-03 Publisher port 必须镜像三种失败语义；
不得统一成事务。

### S0-10 — 温和阈值严格性：breadth 三档全为 strict
**Observed**: strong:`ratio>0.7`、normal:`0.4<ratio≤0.7` 有效边界——
ratio=0.70 恰落 'normal'（非 strong）、0.4 恰落 'weak'（非 normal）。
**锁定测试**: `test_breadth.py::TestBreadthThresholds`（3 above ratios）
**Migration constraint**: classifier 抽取保留严格不等号（禁止 >= 化简）。
