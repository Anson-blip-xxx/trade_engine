# Execution Golden Observations (P4-01)

> Characterization doc for Execution layer behavior, frozen by `tests/execution/` (52 tests).
> Scope: `strategies/shared_executor.py::open_position`, `shared/position_manager.py::_close/_partial_close`,
> algo SL queue, `_round_qty`, sandbox interception. **Zero production changes in P4-01.**

Test files:
- `tests/execution/test_open_execution.py` (21) — open path
- `tests/execution/test_close_execution.py` (17) — close / partial close
- `tests/execution/test_algo_sl.py` (10) — algo SL queue + place
- `tests/execution/test_round_sandbox.py` (4) — rounding + sandbox

Isolation principle: all IO (Binance API / Redis / PG / TG / algo worker thread) is mocked;
the functions under test run real implementations.

---

## E-OBS-1 Open order params (frozen)

`open_position` posts `/fapi/v1/order` with:
`side` (BUY/SELL), `type=MARKET`, `quantity` (rounded), `positionSide=BOTH`.

- Retry once on failure (same params).
- Failure of both attempts → return False, no PM registration.

## E-OBS-2 Open success sequence (frozen)

1. Decision/Risk gates run BEFORE any order (`_analysis_allows_open`, `_drawdown_status`,
   `_was_closed_recently`, min-notional, funding).
2. Gate failure → return False, zero Binance calls.
3. Order success → cancel stale algo SLs for symbol → enqueue new algo SL
   (`_algo_enqueue(symbol, side, trigger, qty)`; LONG→SELL trigger below entry,
   SHORT→BUY trigger above) → register position in PM (`pm:positions` via `_rset`) → TG notify.
4. `open_position` returns True.

## E-OBS-3 Order retry failure keeps PM clean

Both order attempts fail → position NOT written to `pm:positions`, algo SL not enqueued.

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

## E-OBS-11 Sandbox interception points

- SE: `_sandbox_check()` True → `fapi_post` returns mock result (order never sent).
- PM: `_sandbox_active()` True inside `_close` → skip Binance entirely,
  record with `final_close=True`, pnl computed from entry/price directionally.

## E-OBS-12 `_position_id` format (dual-format family, see PM OBS-1)

`_position_id = pos['position_id'] or f"{system}:{symbol}:{entry:.12g}:{open_time:.6f}"`.
Close events (CLOSE_ORDER_FILLED / PARTIAL / EXCHANGE_POSITION_FLAT) all carry this id.
