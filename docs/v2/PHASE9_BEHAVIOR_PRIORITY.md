# Phase 9-00 — Behavior Ticket Prioritization / Golden Audit（docs/tests only；production 0 diff）

> @ `1f9eb00`。P9 不修 bug：统一档案 → 评分 → 依赖图 → Golden 完备度审计 →
> 分层 → **首票建议 + 为什么不是其它候选**。

---

## 一、Ticket Inventory（Phase 8 全部 deferred + inventory 全宗）

| ID | 标题 | 影响域 | 冻结依据 |
|---|---|---|---|
| S0-1 | S0 写 `market_state` / SE 读 `market_mode` → fail-open | 全策略风控闸 | `s0/core.py L81` vs `SE L1098` |
| PMB-9 | algo cancel fallback 槽位实为 GET | Protection | `_algo_cancel` PT-RMB9 |
| PMB-17 | ghost 队列 side=None → filter 失效消费 | Reconcile | PMB-17 |
| PMB-23 | ghost pop-before-record + 全环同生死 | Reconcile | PMB-23 |
| PMB-24 | reconcile silent 通道 | Reconcile | PMB-24 |
| PMB-26 | marker 自愈 / TG 吞错 / PG 传播 不对称 | Reconcile(+merge) | PMB-26 |
| PMB-27 | partial marker persist → recent guard 阻断 | Lifecycle | PMB-27 |
| PMB-29 | `_set_cooldown` noop | Lifecycle | PMB-29 |
| PMB-30 | dup-check 前置孤立 AlgoSL | Lifecycle | PMB-30（≈T1 同根） |
| T1 | duplicate open 后置查重（真实订单先发） | Lifecycle | OBS-3 |
| T5 | 11s SL window（queue 11s） | Protection | E-OBS-3 |
| T12 | save-order symmetry | State/StateService | PMB-28 |
| T14 | dependent-on-T1 | — | — |
| POS-ID | position_id 来源多格式（OBS-1/2） | State/ledger | OBS-1/2 |

## 二、Scoring（1-5）

公式：**RiskScore = Money×3 + Probability×2 + BlastRadius×2 + Verifiability − Complexity**

| Ticket | Money | Prob | Blast | FixCx | Verify | Score | 依据 |
|---|---|---|---|---|---|---|---|
| **S0-1** | 5 | 5 | 5 | 1 | 5 | **34** | risk_off 长期 fail-open（正常路径），key 改 1 行，S0 golden 完备 |
| **T1+PMB-30**（同根 cluster） | 5 | 3 | 4 | 3 | 4 | **29** | 真 order 先于查重；修复 restrict + 顺序（多点验证） |
| **T5 11s SL window** | 5 | 3 | 3 | 3 | 4 | **26** | 11s 裸仓 + backlog 放大；需重构 protection enqueue（非 trivial） |
| **PMB-23** | 4 | 2 | 3 | 3 | 5 | **21** | 失败窗口小概率；两子票（A pop-before-record / B batch isolation） |
| **T12 save-order** | 5 | 1 | 4 | 5 | 3 | **23** | high complexity / crash-consistency |
| **PMB-9** | 4 | 2 | 2 | 2 | 5 | **20** | cancel fallback GET bug；deterministic；局部 |
| **PMB-27** | 3 | 2 | 2 | 2 | 5 | **17** | recent-guard 阻断明显面；可验证性高 |
| **PMB-17** | 3 | 1 | 2 | 2 | 5 | **17** | 边缘 |
| **POS-ID unify** | 2 | 2 | 2 | 3 | 4 | **13** | architecture normalization（不 defect） |
| **PMB-26** | 2 | 2 | 2 | 3 | 5 | **18** | 多面：A marker自愈 / B TG 吞错 / C PG 传播（拆票） |
| **PMB-24** | 2 | 1 | 2 | 2 | 5 | **14** | NEEDS_SEMANTIC_DECISION |
| **PMB-29** | 1 | 1 | 1 | 1 | 5 | **10** | NO CHANGE 风险低 |

## 三、Dependency Graph

```
T14 ──▶ T1（同根 PMB-30；不合并票但记录 cluster）
T5   ──▶ Protection worker/sleep（不依赖 C1）
PMB-9 ──▶ Protection cancel（可独立；与 C1 不绑死——只注接口）
PMB-23 拆票 A/B（独立可执行）
S0-1 独立（单行 key alignment，非 IO ticket）
POS-ID ↔ reconcile/migrate/ledger（normalization，放后）
```

## 四、Golden Coverage Matrix

| Ticket | Coverage | Note |
|---|---|---|
| S0-1 | **PARTIAL** | s0 core/s6_allowed golden 完备；SE `market_mode` default 分支未 frozen（修前补） |
| T1/PMB-30 | **FULL** | open golden + dup-order + orphan SL 冒烟 |
| T5 | PARTIAL | worker sleep 11 frozen；裸仓窗口风险未 explicit golden |
| PMB-23A | FULL | pop-before-record golden 化 |
| PMB-23B | PARTIAL | 全环中止（外层 catch）冻结 |
| PMB-9 | MISSING | fallback GET 路径需补 characterization（修前必补） |
| PMB-27/29/24/17 | PARTIAL | 冻结行为；semantic decision 未确认 |
| T12 | PARTIAL | save-order 三分支冻结；无 crash-consistency 端到端 |
| POS-ID | PARTIAL | 两格式 frozen |

## 五、Fix Readiness

| Ticket | Readiness |
|---|---|
| S0-1 | **READY**（key alignment + 一处 SE 读；需补 1农场 golden） |
| T1/PMB-30 cluster | NEEDS_PRODUCT_DECISION（提前查重 vs orphan orphan） |
| T5 | NEEDS_GOLDEN + PRODESIGN（同步 protection） |
| PMB-23-B | PARTIAL BLOCKED（batch isolation 需 intent decision） |
| PMB-9 | NEEDS_GOLDEN（先补 characterization） |
| PMB-27/17 | READY（低 blast） |
| PMB-24 | NEEDS_PRODUCT_DECISION |
| PMB-26 | NEEDS_SEMANTIC_DECISION（拆票） |
| PMB-29 | NO RISK / defer |
| POSIX-ID | ARCHITECTURE / defer |
| T12 | NEEDS_COMPLEXITY_REVIEW |
| PMB-26 | NEEDS_PRODUCT_DECISION |

## 六、Priority Tiers

- **P0**：S0-1（唯一高 Money/Prob/Blast **简单一行 key 对齐** + 测试完备）→ **FIXED / CLOSED（P9-01B，commit `8ee0ea5`）**
- **P1**：T1+PMB-30 cluster；T5 11s SL；PMB-9
- **P2**：PMB-23（A/B 拆票）；PMB-27；PMB-17；PMB-26（拆票）
- **P3**：PMB-24（NEEDS_DECISION）；PMB-29（NO RISK）；POS-ID（normalization）

## 七、首选票建议：**P9-01 = S0-1 修复**

**为什么是它，而不是另外四个？**
- vs **T1/pmb-30**：真实资金风险同级，但 T1 需要 product intent 决策（提前查重策略）
  + multi-module ordering + orphan recovery——无 READY 态。
- vs **T5**：高风险但需 protection architecture 重 cancellationToken；非孤立修复。
- vs **PMB-9**：单点局部，但风险窗口较窄且需先补 characterization（NEEDS_GOLDEN）。
- vs **PMB-23**：失败窗口小、触发概率低；分拆两票后才能动。

**S0-1 = key alignment （one-line）+ Golden patches**，P0/READY。
## 八、Yellow Architecture 分离

C1/C6 与 behavior ticket **分开**打分；PMB-9 不需等 C1（_fix at same layer now）。

## 九、Rollback Strategy

一票一 commit；Golden 先行（check 即为修复测试），roll back = revert one
commit。Risk Tier 分 P0/P1/P2/P3；首票 S0-1 READY。

## 十、（backup doc only）
每票的 Current vs Expected / 12 criteria 详表落
`docs/v2/PHASE9_BEHAVIOR_PRIORITY.md`（本文件）。
