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

For A -> B, A becomes DRAINING without changing its binding/token. The transport
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
