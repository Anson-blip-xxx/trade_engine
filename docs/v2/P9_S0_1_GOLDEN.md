# P9-01A — S0-1 Reader Characterization（冻结当前 bug）

## 一、Producer contract（真实 HEAD）

- Redis key：`market:s0`（`s0_service.publish_state` 三写：Redis/file/CH）
- producer核心 = `s0/core.classify_regime` 返回 dict（含
  `"market_state": market_state`，无 `market_mode` 键）
- `market_state` 取值（vocabulary）：`risk-off` / `trend` / `range`（连字符小写）
- 失败：Redis/file/CH per-write；CH 失败仅 log（镜像 S0-9 冻结）
- 无 TTL

## 二、Reader contract（SE `market_allows_trading`）

- `get_market_state()` → `_rget('market:s0')`（异常吞错→{}）
- 只识别 `ms.get('market_mode', 'normal')`
- `mode == 'risk_off'`（下划线）→ False；其它（'normal'/'bear'/unknown/
  `None` payload/`{}`/`market_state` only/unknown/exception）→ **True（fail-open）**
- fetch 一次（`test_call_count_frozen`）

## 三、核心 mismatch 重现（deterministic bug test）
`test_producer_shaped_payload_fail_open_now`：produer-shaped payload！(只写
`market_state`) → reader 进入 `normal` 缺省 → 允许开仓。

## 四、Conflict / fallback / legacy matrix

| payload | reader 行为 |
|---|---|
| producer-shape（只 market_state='risk-off'） | **允许（BUG）** |
| market_mode='risk_off' | **阻断** |
| market_mode='normal' | 允许 |
| {}/None | 允许 |
| 两个 key 同写 conflict（market_state='risk-off' | 还允许 = normal dominate **legacy key wins** |
| both keys conflict (market_state vs market_mode 同时存在) | legacy `market_mode` 唯一 authority |
| unknown value | 允许 |
| fetch exception | 允许 |

## 五、Vocabulary Comparison

| producer 写 | reader 识别 |
|---|---|
| `risk-off`（连字符） | `risk_off`（下划线）→ 语义缺失 |
| `trend` / `range` | 无对应（reader 只认 risk_off）|

**结论**：S0-1 = **key + vocabulary 双 mismatch**（key 撞 `market_mode`↔`market_state`；
值域 `risk-off` 连字符 vs `risk_off` 下划线）。P9-01B **只改 reader key**，
vocabulary 并入同票：reader 读 `market_state` + 判 `in ('risk-off','risk_off')`→ 待 P9-01B 按最小语义策略（proposal：canon `risk-off`）。

## 六、Repository-wide `market_state/market_mode` 矩阵

- **producer**：`s0/core.py`（写 `market_state`，唯一 source）
- **consumer**：`strategies/shared_executor.py:1098`（读 `market_mode` — bug 唯一位）
- **reader helper**：`services/s0/s0_reader.py` / `services/s7/s0_reader.py`
  （正确读 `market_state` — 语义镜像）
- **test**：`tests/s0/*`、`tests/phase9/*`、`tests/execution/test_phase9_*`
- 仅 SE 一处写/读 mismatch；无其它 producer 写 `market_mode`——**key 对齐即足**。

## 七、Blast radius

`market_allows_trading` 调用点：`strategies/S6.py`（LONG）/`strategies/S8.py`
（SHORT）**仅 open 闸门**；close/partial 链不受影响（monitor/reconcile 不经它）。

## 八、P9-01B Intended Delta（计划，未改）

- `shared_executor.market_allows_trading`：
  `mode = ms.get('market_state', ms.get('market_mode', 'normal'))`
  实现最简且兼顾 legacy：
  - producer payload（新 key）直接生效
  - 若残留 legacy `market_mode` 数据则降级旧语义
- 无清除 backlog 其它 key；风险闸触发 value：`risk-off`（hyphen）**+
  legacy `risk_off`**（保留 defencive；记 ticket PMB 新增值比较——不温和统一）
- 一 reader seam、一 commit（回滚 revert 单 commit 恢复 fail-open）。

## 九、Rollback boundary

P9-01B 单 commit 行为改动（reader seam 一处）；
异常 revert `git revert 8ee0ea5` 恢复原 fail-open。


---

# FIXED（P9-01B）

- Commit `8ee0ea5`：`fix(v2): align executor S0 market-state gate`
- **OLD behavior**：SE 读 `market_mode` → producer 只发 `market_state`，
  producer-shaped `risk-off` fail-open 允仓（S6/S8）。
- **NEW behavior**：`mode = ms.get('market_state', ms.get('market_mode','normal'))`；
  value `in ('risk-off','risk_off')` → BLOCK。`market_state` authoritative
  （presence != truthiness；None/empty 不逆 fallback）；legacy `market_mode`
  仅在 `market_state` 缺失时 fallback。
- **authority rule**：单 key authoritative（非 OR 双 source-of-truth）
- legacy fallback：`{'market_mode':'risk_off'}` 继续阻断 / 'normal' 允许
- value compat：`risk-off`（连字生产 vocab）与 `risk_off`（legacy）都阻断；
  'trend'/'range'/unknown → 允许（不 normalization；唯一字面值集合）
- blast radius：S6/S8 open 闸门（无 close/partial 变化）
- rollback：`git revert 8ee0ea5` 单 commit 恢复旧 fail-open（无 schema/Redis
  cleanup/producer coordination）
