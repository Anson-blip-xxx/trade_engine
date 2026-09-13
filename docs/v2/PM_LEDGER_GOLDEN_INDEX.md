# PM Ledger Golden Index（P7-03A）

> 映射 Golden ID → ledger area → test file → P7-03B LedgerService 约束。
> 真实链调用图（重新核实 @ `1d54200`），legacy `_rget` 无 stale gate。

---

## 1. 真实 Ledger Call Graph（重新核实）

### A. Open
```text
Binance order 成功
  → PM state（Redis pm:positions + _POS_CACHE）
  → PG trade_events(OPEN_ORDER_FILLED via `_pg_record_event` ← shared_executor L1011)
  → TG → Algo enqueue
```
**Open 不产生 trade_episodes 行**（episode 只在 final close 时 upsert）。

### B. Full Close（PM orchestration 侧）
```text
mark_closed → positionRisk#1 → order(Service) → positionRisk#2
  → (flat path?) _cancel_all_algo → pg(CLOSE_ORDER_FILLED via rec order)
  → record_trade(final_close=True ← trade_recorder 真管账)：
      [非重复判定(x) → income 公式覆盖(PMB-7) → redis partial merge
       → PG trade_episodes UPSERT → CH trade_history → loss cooldown
       → analysis enqueue → TG]
  → positions.pop + _save
```

### C. Partial Close
```text
execution → qty 更新 → Redis save（无 PG / 无 CH / 无 record_trade）
（**PMB-6 冻结**：partial 完全无 ledger —— record_trade 拿 final_close=False
  时也仅写 redis partial 累积 key `trade:partial:{sha1}`，无 PG/CH）
```

### D. Exchange Flat Path
```text
flat path（positionRisk 无该 symbol）→ cancel → record_trade(final=True)
  → PG/CH 照写（与 full close 保持相同 recording path，不遗漏）
```

---

## 2. Golden Constraints per P7-03B

| Area | Test file | Migration constraint |
|------|-----------|----------------------|
| full close payload contract | test_ledger_recording_golden.py::TestRecordTradeEpisodePayload | 21 字段名+类型逐字保留；episode 位置映射冻结 |
| event-tier PnL 公式 | 同上::test_episode_pnl_formula_short_win | event(公式) vs episode(income) **两套分别写**（PMB-7 禁统一） |
| short-loss / long-win | 同上 | 符号逻辑逐字（entry-exit 等）|
| qty=0 skip | test_record_episode_payload::test_qty_zero_silent_skip | 保留 |
| missing position_id 合成 (`.12g`+`.6f`) | 同上::test_missing_position_id_synthesized | position_id 双格式（PM OBS-1 family）不统一 |
| episode income reconciliation | TestIncomeReconciliationPMB7 | 无 income → 保留公式；非零 income → **覆盖公式**；income raise / 畸形 → 吞错 → 保留公式值 |
| partial close 无 PG/CH | TestPartialCloseLedgerPMB6 | **PMB-6 冻结**：不许补 ledger |
| full close + partial merge | TestFullCloseRecording | final=True 时 flush partial（累积基数 4+6=10/加权 exit 1.9）|
| recording 顺序 | test_ledger_ordering_golden.py::test_pg_then_ch_order_frozen_clean | pg → ch → analysis（含 TG），禁 sort |
| pg raise → analysis skipped | TestFailureTopology::test_pg_exception_swallowed | PG raise → 整条 analysis 跳过 |
| CH raise → pg written/an skip | TestFailureTopology::test_ch_failure_analysis_skipped | CH raise → analysis 跳过但 PG 已写入 |
| call counts | TestRecordingOrder::test_call_counts_frozen | full: 1+1+1；partial: 0+0+0 |

## 3. PositionLedgerPort sufficiency（P7-03B 输入）

- `record_trade_event / upsert_trade_episode` 来自 P4-D3 Port ✓ 已冻结。
- **缺口**：record_trade 内还引发：`CH insert` / `_ch_query`（dedup & stats）/
  `income` 公式覆盖（经 `fapi_get`）/ `loss cooldown redis` / `enqueue_closed_trade`。
- 结论：P7-03B 中 **LedgerPort 不足以承担全部 record_trade 语义**；需要
  LedgerService 以口要注入 CH/income/analysis/tg callables，
  **而 record_trade 的 income 嵌套 Binance 调用保留在 wrapper 内**
  （P7-03B 接入仅以 callable/memory 注入 helper 方法式签名——不另开 Port）。
