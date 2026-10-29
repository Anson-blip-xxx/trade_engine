# Testnet drawdown observation — 2026-10-11

## Scope and authorization

User requested Testnet drawdown protection be suspended to collect samples.
Applied only to the currently selected Testnet account (alias Xu); no LIVE
configuration, credential, equity baseline, historical PnL, or queue deletion.

## Implementation

- Account-scoped PostgreSQL BusinessState namespace
  `capital-drawdown-control-v1`, key `active`, audited CAS version 1.
- `v2_core.drawdown_observation.configure` is an explicit operator control,
  serialized with the account protocol lock. No general web state-write grant.
- Disabled mode is `OBSERVE_ONLY`, capital drawdown factor 1. Both cumulative
  drawdown recovery braking and legacy drawdown sizing reductions are bypassed.
  Legacy measurements, capital high-water, anchor and losses remain recorded.
- Existing recovery state is retained as `suspended_recovery`. Re-enabling does
  not turn observation-only trades into successful probe recovery. Previously
  paused/probing accounts return to HALTED for review, as do previously halted
  accounts according to existing recovery logic.
- Explicit SANDBOX scope and enabled capital model / portfolio risk guards are
  required. Absent control defaults to protection enabled; malformed controls
  fail closed. Other accounts are unchanged.
- No new runtime-policy schema key is persisted: existing dashboard/monitor
  policy readers remain compatible. Policy version 6, capital pool 0.70 remain
  unchanged. Capital exhaustion, individual stops, margin/risk limits, exposure
  reconciliation and uncertain-order protections are not bypassed.
- Account console renders the new mode and explains retained protections.

## Verification

- Workspace isolated PostgreSQL tests: 80 passed.
- Candidate built from the actual deployed release with only three runtime
  modules changed: 56 isolated PostgreSQL tests passed. Initial candidate QA
  was blocked by legacy parent conftest requiring a missing Binance env file;
  rerun with `--confcutdir=tests/v2_core` selected the V2 fixtures. No credentials
  were copied into QA, and no live database was used by tests.
- Frontend behavior and Chromium 390/1440 viewport tests: 3 passed, including
  observation-only display and disabled recovery form.

## Deployment and observed acceptance (UTC+8)

- Engine: `/opt/trade-engine-v2/releases/drawdown-observation-20261011-v1`,
  based on `signal-latency-20261008-v6`. Workspace has unrelated unpublished
  changes, deliberately excluded from this release.
- Admin: `/opt/trade-engine-v2/releases/drawdown-account-admin-20261011-v1`,
  based on `risk-recovery-account-admin-20261010-v1`; one JS asset changed.
- Controller restarted at 16:59:53 after observing no active episodes,
  zero exchange margin use and clear coverage. Same-account/binding request
  epoch 42 passed controller preflight; route ACTIVE with no blockers.
- Real capital snapshot: factor 1, OBSERVE_ONLY, control version 1;
  base 973.04842547, peak 1040.92986434, drawdown approximately 6.5212%.
  Anchor and high-water unchanged. Existing HALTED recovery retained.
- Authenticated account recovery GET: HTTP 200 with OBSERVE_ONLY.
- Pipeline completed cycles and consumed signals after restart. No forced
  order was submitted to prove this configuration change.
- Signal-consumption lag findings were observed during restart recovery, then
  the final health read returned empty findings and no active alerts. This
  change does not fix or relax consumption-lag monitoring. No new trade intent
  had been created by this acceptance read; process health and these tests are
  not proof of a new end-to-end completed trade.

## Rollback

Use `configure` for this same SANDBOX account with `enabled=True`, the currently
observed control version, and a review reason. Wait for a fresh snapshot and
verify protection state. Do not change capital anchor or recovery attempts.
If code rollback is necessary, restore the previous release symlink and use the
normal controller restart / same-binding resume protocol. Old code ignores
the control and enforces drawdown again; do not confuse that with lost history.
