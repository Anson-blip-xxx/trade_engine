# Execution Golden Observations (P4-01)

> Characterization doc for Execution layer behavior, frozen by `tests/execution/` (60 tests).
> Scope: `strategies/shared_executor.py::open_position`, `shared/position_manager.py::open_position/_close/_partial_close`,
> algo SL queue, `_round_qty`, sandbox interception. **Zero production changes in P4-01.**

Test files:
- `tests/execution/test_open_execution.py` (24) — open path + no-retry freeze
- `tests/execution/test_close_execution.py` (12) — close / partial close + full call sequences
- `tests/execution/test_algo_sl.py` (10) — algo SL queue + place (real impl)
- `tests/execution/test_round_sandbox.py` (14) — rounding + sandbox (incl. real interception)

Isolation principle: all IO (Binance API / Redis / PG / TG / algo worker thread) is mocked;
the functions under test run real implementations. Real-network tests fake only the lowest
layer (`requests.post`/`requests.get`) — the interception logic itself is real.

---

## E-OBS-1 Open order params (frozen)

`se.open_position` posts `/fapi/v1/order` with:
`symbol`, `side` (BUY/SELL), `type=MARKET`, `quantity`, `newOrderRespType=RESULT`.
No `positionSide`, no `reduceOnly` (open order).

`pm.open_position` posts `/fapi/v1/order` with:
`symbol`, `side`, `type=MARKET`, `quantity`, `positionSide=BOTH`.

## E-OBS-1a Open order failure = single attempt, NO retry (CORRECTED)

Verified against real code (se L944-953, pm L796-802): **there is no retry-once**.
Earlier draft of this doc claimed "Retry once on failure" — that was WRONG; corrected here.

- Order rejected (`result` falsy or `result.code` truthy) → exactly **1** order call
  with identical params → return False.
- Order returns None → exactly **1** order call → return False.
- `pm.open_position` order raises → exactly **1** order call → return False.
- In all cases: no PM registration, no PG event, no TG, no algo enqueue.

## E-OBS-2 Open success sequence (frozen)

1. Decision/Risk gates run BEFORE any order (`_analysis_allows_open`, `_drawdown_status`,
   `_was_closed_recently`, min-notional, funding).
2. Gate failure → return False, zero Binance calls.
3. Order posts in fixed sequence: `/fapi/v1/leverage` → `/fapi/v1/marginType` →
   `/fapi/v1/order`. **No stale-algo-SL cancel in `open_position`** (cancel
   happens only in `_algo_place_sl_inner` before placing, and in error paths
   as `cancelOrder` for unfilled/partial fills).
4. Order success → parse fill (executedQty/cumQty/avgPrice; avgPrice≤0 falls back
   to entry_price) → `_update_pos_cache` (pm:positions via `_rset`; failure →
   `cancelOrder` + return False) → PG `OPEN_ORDER_FILLED` → TG notify →
   `_algo_enqueue(symbol, side, trigger, qty)` (LONG→SELL below entry,
   SHORT→BUY above; filled_qty used, not requested qty).
5. `open_position` returns True.

## E-OBS-3 Order failure keeps PM clean

Single order attempt fails → position NOT written to `pm:positions`, no PG event,
no TG, algo SL not enqueued (see E-OBS-1a: no retry exists).

## E-OBS-4 Cross-test pollution hazard (test-infra OBS)

`shared_executor` and `position_manager` are mutated globally by production code paths
(e.g. `se.fapi_post = ...` style assignment in older tests). Tests MUST use
`monkeypatch.setattr`; direct module attribute assignment leaks into later tests
(observe: 3 open tests failed only when run together, passed individually).

## E-OBS-5 Close order params include reduceOnly (asymmetry vs partial)

`_close` posts `/fapi/v1/order` with `reduceOnly: 'true'`;
`_partial_close` does **NOT** pass `reduceOnly` (only `positionSide=BOTH`).
This asymmetry is current behavior — frozen as-is, flagged for P5 review.

## E-OBS-6 Close flow (frozen)

1. `_was_closed_recently` guard (4h cross-process marker) → skip if recently closed.
2. `_mark_closed` BEFORE any exchange call (marker-first; OBS-9 family).
3. Sandbox mode → no Binance calls, direct record_trade(final_close=True), pop+save.
4. Real mode: query `positionRisk`; if flat → EXCHANGE_POSITION_FLAT pg event,
   record final_close=True, pop+save (no order posted).
5. Market close order → re-query positionRisk:
   - remaining ≥ 0.001 → CLOSE_ORDER_PARTIAL event, record final_close=False,
     keep position with remaining qty, return False.
   - flat → CLOSE_ORDER_FILLED event with full payload → record final_close=True → pop+save.
6. Rejected order (`result.code` truthy) → `_clear_closed_marker`, return False, keep position.
7. Exception → `_clear_closed_marker`, return False.
8. `_cancel_all_algo` only after successful market close (not before) — except in the
   exchange-already-flat branch, where it runs before recording.

### E-OBS-6a Exact call sequences (spy-verified, frozen)

Full-close branch (real order emitted), spy order:
```
mark_closed → positionRisk#1 → order(reduceOnly=true) → positionRisk#2
→ cancel_algo → pg_record(CLOSE_ORDER_FILLED) → record_trade(final=True) → save
```

Exchange-already-flat branch, spy order:
```
mark_closed → positionRisk#1 → cancel_algo
→ record_trade(final=True) → pg_record(EXCHANGE_POSITION_FLAT) → save
```

Differences frozen as OBSERVED (not fixed):
- flat branch has NO order and NO second positionRisk query;
- **record_trade runs BEFORE pg_record in the flat branch** (order is inverted
  vs the full-close branch where pg_record precedes record_trade).

## E-OBS-7 Partial close negative qty inverts position (OBS-4/OBS-7 family)

`_partial_close` with close_qty < 0 → `pos['qty'] = qty - (-|x|)` INCREASES qty
(10 - (-100) = 110). No validation. Frozen as observed behavior.

## E-OBS-8 Partial close has no record_trade / pg event (OBS-8)

`_partial_close` success path: order → qty decrement → `_save`. No trade record,
no PG event, no algo SL update. Frozen as observed.

## E-OBS-9 Algo SL place (real impl, frozen)

`_algo_place_sl_inner`:
1. Fetch exchangeInfo via raw `requests.get` (public API, no auth) → round qty by
   LOT_SIZE stepSize (floor via modulo), trigger by PRICE_FILTER tickSize.
2. `_cancel_all_algo(symbol)` FIRST (dedupe against restart accumulation).
3. POST `/fapi/v1/algoOrder` with frozen params: positionSide=BOTH,
   algoType=CONDITIONAL, type=STOP_MARKET, workingType=MARK_PRICE,
   timeInForce=GTC, reduceOnly=true.
4. Success → write `algo_sl_id` into PM positions JSON; failure → log only.
5. Exceptions → `{'error': str(e)}` (never raises to caller).
Queue: `_algo_enqueue` appends FIFO tuples `(symbol, side, trigger, qty)`;
worker consumes at 11s rate-limit intervals.

## E-OBS-10 Rounding semantics differ between SE and PM

- `se._round_qty`: exchangeInfo LOT_SIZE stepSize → floor-to-step
  (e.g. 17508.9 with step=1 → **17508.0**, truncation not rounding).
  exchangeInfo failure → return raw qty (fail-open).
  Symbol missing from exchangeInfo → return raw qty.
- `pm._round_qty`: uses `_s6api().get_symbol_info` (qty precision) → `round(qty, prec)`.
  Failure → `round(qty, 6)`.
Two different rounding regimes coexist; frozen as-is, flagged for P5 unification.

## E-OBS-11 Sandbox interception points (real-path verified)

- SE: `_sandbox_check()` True → `fapi_post` returns mock result (order never sent).
- PM: `_sandbox_active()` True inside `_close` → skip Binance entirely,
  record with `final_close=True`, pnl computed from entry/price directionally.

### E-OBS-11a Real `se.fapi_post` interception semantics (frozen via real-path tests)

Verified by faking ONLY `requests.post`/`requests.get` (real `fapi_post`,
`_sandbox_post`, `scripts.sandbox.mock_post_order` all run for real):

1. **Intercept condition is path-based**: `_sandbox_post` intercepts only paths whose
   lowercase contains `order`. So BOTH `/fapi/v1/order` AND `/fapi/v1/algoOrder`
   are intercepted in sandbox mode.
2. Intercepted → `scripts.sandbox.mock_post_order(params)` runs (its own
   `is_active()` gate is independent of `se._sandbox_check` — env `SANDBOX=1`
   or marker file) → writes sandbox state file → returns mock fill
   (`status=FILLED`, `executedQty`, `avgPrice` from seeded price) or mock algo
   id (`algoId`) for CONDITIONAL STOP_MARKET.
3. **Non-order paths are NOT intercepted** even with sandbox ON:
   `/fapi/v1/leverage` etc. fall through to the real network layer
   (append `timestamp` + `signature`, header `X-MBX-APIKEY`).
4. Sandbox OFF → full network path: params dict gains `timestamp` + `signature`
   **in place** (the caller's dict is mutated — side effect frozen), request sent
   via `requests.post(f'{FAPI}{path}', ...)`.
5. `_sandbox_get` intercepts `positionRisk` / `account` queries analogously.

## E-OBS-12 `_position_id` format (dual-format family, see PM OBS-1)

`_position_id = pos['position_id'] or f"{system}:{symbol}:{entry:.12g}:{open_time:.6f}"`.
Close events (CLOSE_ORDER_FILLED / PARTIAL / EXCHANGE_POSITION_FLAT) all carry this id.
