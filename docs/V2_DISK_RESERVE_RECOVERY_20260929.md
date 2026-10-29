# Disk reserve incident and recovery — 2026-09-29

All displayed times in this report are UTC+8. Scope: SANDBOX host maintenance;
no trading configuration, account routing, broker credentials or ledger changes.

## Cause and immediate recovery

The market collector's last successful heartbeat was 10:19:37. Read-only
inspection at 16:11–16:18 found 5.75 GiB free against the unchanged 8 GiB
minimum. `MARKET_DISK_RESERVE_REQUIRED` stopped collection; stale market
observations subsequently blocked entry. This was not a settlement precision
regression. Approximately 72.54 GiB in 437 historical `/tmp/v2-data-qa.*`
directories had accumulated. Market process output was not available in the
journal, making independent persisted diagnostics necessary.

Eight individually checked, cleanly stopped disposable PostgreSQL directories
were removed first. Market collection resumed without a service restart;
heartbeat was CURRENT at 16:34:57. Automated cleanup subsequently removed
352 additional verified database subdirectories in batches of 256 and 96.
Total removed: 360 disposable QA databases. Free space at final check was
76.29 GiB. Logs and existing archives remain. Deleted unarchived QA databases
cannot be directly restored; tests can recreate them. No business databases,
Redis state, active database directories or trading records were removed.

Ten directories with unrecognised cluster identity and one too-recent directory
were retained. Final dry-run: 428 NO_DATABASE, 10 WRONG_CLUSTER_IDENTITY,
1 TOO_RECENT, zero ELIGIBLE. Two new QA runs account for the changed directory
total. Raw before/after manifests: `/tmp/v2-host-maintenance.iKA9RH/`.

## Prevention now deployed

Independent release: `/opt/trade-engine-v2/releases/host-maintenance-20260929-v1`,
accessed through `/opt/trade-engine-v2/host-maintenance`. Built from the running
precision-contract release plus only the maintenance allowlist. Unrelated dirty
workspace changes and fake QA credentials were not deployed.

- `trade-v2-qa-gc.timer`: hourly, persistent, maximum 256 removals per run.
  Collector defaults to dry-run outside this explicitly configured service.
  Only same-owner, older-than-24h, cleanly stopped PostgreSQL 16 clusters with
  the exact isolated-QA identity, local socket and port 55443 qualify. Live
  process references, PIDs/sockets, keep markers, symlinks and nested mounts
  prevent removal. Eligibility is rechecked immediately before deleting only
  the `pg` child. Root execution is refused. The runner and collector share a
  host lock. Cleanup runs with idle I/O priority and nice 15.
- `trade-v2-host-disk-health.timer`: every minute; persists current disk and
  market freshness in PostgreSQL (`host-disk-health-v1`). Uses the live market
  policy's reserve and freshness limits. Warning starts below 16 GiB and clears
  at 18 GiB; the existing 8 GiB trading safety reserve is unchanged.
- Warning/recovery delivery uses the existing encrypted notification credential
  and an explicit positive private-DM destination. No trading credentials are
  loaded and no fallback to the trading group is permitted. Acknowledgements
  in `watchdog-delivery-v1` deduplicate normal repeated checks and restarts.
  Send-before-persist remains at-least-once: a crash or database failure after
  Telegram acceptance can cause a retry. Failed assessment never signals a
  false recovery. Telemetry failures exit nonzero for operational inspection.
- Non-secret warning settings are in
  `/etc/trade-engine-v2/host-maintenance.env`. Both timers are enabled at boot.
  Services are oneshot; an inactive service after successful completion is
  expected. Inspect timer activity, exit status and persisted snapshots together.

## Verification, not merely process liveness

- Exact release candidate: **96 tests passed, no skips** in a disposable
  PostgreSQL environment. Coverage includes deletion exclusions/races,
  runner cleanup and locking, thresholds/hysteresis, missing/stale heartbeat,
  notification persistence/retry, DM formatting, existing watchdog and trade
  notification regression. Ruff and scoped diff whitespace checks passed.
  Initial fixture issues (missing market migration and invalid dummy Telegram
  token) were corrected before the passing candidate run.
- Real LOW DM was acknowledged at approximately 11.1 GiB. A subsequent check
  did not increase its notification acknowledgement revision. The timer later
  delivered RECOVERED; acknowledgement advanced from revision 1 to 2 with an
  empty reported set, and remained revision 2 on another automatic check.
- Both maintenance services completed with exit status 0. The minute timer's
  observed executions advanced persisted snapshots. The hourly cleanup timer
  is armed; its cleanup service has completed two manual initial runs. The next
  scheduled hourly run was not awaited during this acceptance.
- Trading controller and market hub kept their original PIDs and release
  symlinks, with zero service restarts.
- At 17:27:30 market heartbeat was CURRENT. Latest completed pipeline finished
  17:27:19: CYCLE_COMPLETE, entry_dispatch_enabled=true, market CURRENT,
  protection/exits/settlement CLEAR.
- Natural post-recovery orders, from persisted broker execution records:
  QUSDT opened FILLED at 16:41:38 (exchange order 281044125); QNTUSDT opened
  FILLED at 16:45:24 (210950654), closed FILLED at 16:48:44 (210952515).
  QNTUSDT settled net PnL 3.4550544 USDT. No manual test order was submitted.

## Remaining observations / limits

A separate SIGNAL_CONSUMPTION_LAG finding still covered five S6/S3 signals at
17:26:23. It was not a global entry blocker; fresh pipeline cycles completed.
This incident fix does not claim all queue latency or strategy rejection causes
are solved. Nor does it promise that a strong signal always results in an order.
Unrecognised QA directories were deliberately retained for separate review.
Monitoring is currently explicitly SANDBOX-scoped; production rollout needs its
own environment-aware monitoring configuration. No live-market rollout occurred.
