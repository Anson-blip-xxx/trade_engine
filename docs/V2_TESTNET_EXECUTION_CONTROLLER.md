# Independent Testnet process controller

Web appends desired-account requests only. A separate non-Web database role owns
actual route transitions. The supervisor holds a session advisory lock for its
lifetime and launches a fresh Python child per immutable account binding.
Systemd uses KillMode=control-group and encrypted master/notification credentials.

Before a target starts, signed read-only inventory checks mode, balance, positions
and orders twice; accountConfig must report canTrade. Local episodes/orders must
also be clear. The new account gets its own scoped risk/policy baseline; existing
policy, PnL and high-water state are not reset when revisiting an account.

STARTING does not permit mutations. The child loads encrypted credentials, checks
Redis/ClickHouse and composes the real trading pipeline before READY. Only then
does the controller mark ACTIVE and release its start gate. In acceptance mode
new entries are disabled explicitly; exits/protection remain separately gated.

For A -> B, the target is preflighted before touching a healthy ACTIVE source.
A failed target request is recorded as REJECTED while A retains its exact worker
and authority. After a successful preflight A becomes DRAINING without changing
its binding/token. The transport
blocks OPEN and settings, while the child disables admission on its next cycle
and continues recovery, protection, exits and settlement. B cannot start until A
has no positions/orders/unsettled local episodes and the old process is confirmed
stopped. The target is checked again, not assumed ready from an earlier receipt.
This does not force liquidations or silently cancel unknown exchange outcomes.

If a process dies or the controller restarts, the old token is revoked and that
source is recovered in DRAINING mode, never automatically re-enabled for entries.
A subsequent explicit request is required to resume opening. Failed preflight or
startup stays BLOCKED; a new request is required to retry. LIVE requests are
retained for visibility but never consumed by this Testnet supervisor.

Production execution is still not implemented by this deployment: its worker,
separate database/cache and acceptance remain distinct requirements. Do not infer
LIVE readiness from the presence of a LIVE selector or stored API credential.

## Deployment acceptance — 2026-09-27 (UTC+8)

The independent `trade-v2-execution-controller.service` and matching Web release
are deployed. The legacy daemon/watchdog remain disabled. Entries are explicitly
disabled by the root-owned controller configuration; ACTIVE is not evidence that
strategy opening is enabled. API credentials remain in the encrypted vault.

Real Testnet checks confirmed:

- Account A reaches ACTIVE after child READY and composes the actual pipeline.
- A request for account B is REJECTED with NO_AVAILABLE_BALANCE; A remains ACTIVE
  at the same epoch. B's signed wallet, margin and available balances are zero.
- Restarting the controller recovers A in DRAINING with RECOVERING_SOURCE.
- An explicit subsequent request resumes A as ACTIVE, still with
  ENTRY_DISABLED_ACCEPTANCE. Recovery never automatically enables entries.

The isolated PostgreSQL regression run passed 88 tests covering controller,
route locking/fencing, signed transport, credential console/acceptance, runtime
credentials and process composition. This is not a successful A -> B -> A
exchange handover: B requires simulated funds before that acceptance can finish.
No LIVE credential was used and no production runtime was activated.

### Follow-up after Testnet funding

After the owner funded both accounts, actual handover completed A -> B -> A:
controller audit records show epoch 6 ACTIVE for B, then DRAINING/STOPPED,
and epoch 7 STARTING/ACTIVE for the original A. Both activations retained
ENTRY_DISABLED_ACCEPTANCE. This validates funded, flat-account process handover,
not new-order execution or handover with live positions. The earlier zero-balance
blocker is resolved. No production activation was performed.

The Web now places the Testnet execution switch at the top of `/accounts`, with
an explicit target, confirmation, manual refresh and automatic status refresh.
Statistics selection is separate from execution switching. Controls no longer
depend on overview loading; LIVE activation remains disabled in the UI.
Fourteen focused frontend/controller/route tests passed for this update.
