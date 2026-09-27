# Dynamic admission restart acceptance — 2026-09-27 (UTC+8)

## Defect and minimal fix

The dynamic-universe scheduler intentionally avoids market/account context reads
for events unsupported by a directional consumer. Admission nevertheless checked
`universe_admission` before its normal unsupported-event ignore. Real `HIGH_VOL`
and opposite-direction events therefore raised
`CONTRACT_ADMISSION_EVIDENCE_REQUIRED`. Their UNAVAILABLE results blocked the
batch's dispatch gate. This was a code defect, not evidence of insufficient
account balance or excessively strict scoring thresholds.

Release `5544dad` routes unsupported events through the existing replay evaluator
first, preserving the conflicting-event-type validation and immutable IGNORED
receipt. Supported events still require contract eligibility and every existing
freshness, strategy, capital, venue-readiness and protection gate. There is no
blanket suppression of scheduler failures and no threshold relaxation.

New regression coverage includes both S3/TV sources, S6/S8 nonmatching events,
zero context reads for ignored events, terminal task receipts/retries, continued
proof requirements for actionable signals, conflicting event types, and a mixed
pipeline batch where HIGH_VOL no longer blocks a valid PREPARED order.
The final targeted regression passed **168 tests in 91.50 seconds**. An initial
new pipeline test omitted the required fusion evidence; its fixture was corrected
before the clean repeat. Mocked/isolated tests are not exchange execution proof.

## Controlled Testnet retest

The current SANDBOX account was independently verified flat with no ordinary or
conditional orders and a clear local ledger. No account/key switch or LIVE
operation was performed. The current controller release is
`/opt/trade-engine-v2/releases/5544dad-admission-fix`; the market hub and Web
release were not changed for this fix.

An immutable account-scoped PostgreSQL acceptance record, namespace
`testnet-acceptance-v1`, key `restart-fullchain-20260927-r2`, records original
risk/runtime configuration and the test start. Temporary risk version 4 limits
the account to one position, 100 USDT reference notional and a one-hour admission
cooldown. Ordinary capital, signal, leverage and venue gates remain enabled.
The formal same-account control API request reached route epoch 18 ACTIVE.

Initial deployed observations reached `CYCLE_COMPLETE`, with no outstanding
scheduler error. Real nonmatching events now produce
`UNSUPPORTED_DIRECTIONAL_EVENT` receipts instead of poisoning the batch. This
alone does not establish opening, protection, closing or settlement acceptance.

## Final result: partial acceptance, no natural entry

Full isolated V2 regression on this release: **1798 passed in 847.64 seconds**.
Ruff and whitespace checks also passed. No exchange request is made by this QA
suite; do not confuse its fill/exit fixtures with real Testnet execution.

The bounded observer sampled the active route 40 times at roughly 20-second
intervals (about 13 minutes), including the next real TradingView alert batch.
The wider deployment/audit window recorded 27 completed cycles, three blocked
cycles and two unfinished RUNNING journal records across restarts. That wider
window starts before the old worker was stopped. Two transient ValueError task
results were observed and subsequently cleared; their exact cause was not proven.
They are not counted as clean cycles or silently declared fixed by this change.
New tasks no longer showed `CONTRACT_ADMISSION_EVIDENCE_REQUIRED` in the checked
post-deployment windows. There were 751 S3 and five TV unsupported-event ignore
decisions, all retaining durable evidence instead of repeatedly poisoning a batch.

At approximately 19:45 UTC+8, five genuine TV signals were received and consumed:

| Symbol | Actionable consumer result | Verified input |
| --- | --- | --- |
| FLOCKUSDT | IGNORED / strength_below_minimum | strength 20, short threshold 60 |
| CYSUSDT | IGNORED / strength_below_minimum | strength 20, short threshold 60 |
| ACUUSDT | IGNORED / strength_below_minimum | strength 20, short threshold 60 |
| ZESTUSDT | IGNORED / SYMBOL_NOT_TRADING | absent from current Testnet catalog |
| NIULAIUSDT | IGNORED / SYMBOL_NOT_TRADING | absent from current Testnet catalog |

Each opposite-direction consumer recorded UNSUPPORTED_DIRECTIONAL_EVENT rather
than a missing-contract-proof error. No signal was forged, replayed with a new
timestamp or forced past its market/strength/reward-risk gates.

**Zero new intents and zero new orders were produced in this acceptance window.**
Therefore real exchange fills, leverage setup, native protection installation,
closing, settlement, outcome attribution and trade notifications were **not**
qualified by this run. Their isolated tests passed; actual lifecycle acceptance
still needs a qualifying fresh candidate. No profitability or LIVE readiness
claim follows from this result.

## Safe final state

- The corrected controller release remains deployed and healthy. Controller,
  market hub and account-admin all reported active with zero automatic restarts.
- Temporary opening permission was removed: `V2_CONTROLLER_ENABLE_ENTRIES=false`.
  Controller epoch 18 recovered to DRAINING; protection and exits remain enabled.
- Final signed Testnet inventory and local-ledger checks confirmed no positions,
  ordinary orders, conditional orders or unresolved episodes for this account.
- Original account-risk configuration was restored by CAS as version 5. Runtime
  strategy policy remains unchanged at version 1; no entry threshold was relaxed.
- The PG acceptance record ended as `PARTIAL_NO_NATURAL_ENTRY`, including the
  original configurations, restore version, QA counts and observed decisions.
- No account switch, LIVE operation, historical-data deletion or remote Git push
  occurred. The unrelated existing analysis document was left untouched.
