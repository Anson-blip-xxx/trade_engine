# Coverage evidence storage recovery

This repair addresses duplicate PostgreSQL account-coverage evidence, not trade
strategy or risk policy. All displayed operational times use UTC+8. Orders,
fills, cash movements, settlements and original immutable history are preserved.

## Verified cause

On 2026-10-04 the 118 GiB root filesystem had approximately 95–100 MiB available.
The active PostgreSQL database occupied about 89 GiB. Current business state
occupied 36 GiB and state history 39 GiB; most of both was TOAST storage for
coverage snapshots. In contrast, orders, fills and settlements were each below
1 MiB. Decision evidence was a separate 7 GiB contributor, not removed here.

The coverage audit repeatedly collected all historical scoped intents, orders
and protection records. It persisted complete before and after lists even when
equal. BusinessState then stored the complete observation in both current state
and immutable history. The coverage namespace contained 68,920 version-one
observations and 36,548,241,756 bytes of stored current payloads.

Execution controller and market hub were already failed when maintenance began.
Disk exhaustion is confirmed; the exact process-exit traceback was unavailable.
The existing market policy requires an 8 GiB reserve. That limit is not lowered.

## Storage representation

Current coverage evidence now contains only a self-reference to the immutable
history row with the same state identity and version. BusinessState validates
scope, version and reference identity before retrieving the original payload.
Other state namespaces retain their previous representation and CAS semantics.

New audits additionally separate their full ledger facts into immutable,
account-bound content-addressed objects in v2_coverage_blobs. Each record, ordered
64-record chunk and manifest has a SHA-256 identity. Unchanged records are reused
across observations. Inventory and audit result remain in every observation;
before and after contain manifest references. Reads reconstruct the original
structure, including ordering. Missing objects, digest mismatches and crossed
account scopes fail closed. This does not reduce what the safety audit checks.

Fact insertion, evidence history and latest coverage checkpoint share the
original transaction. A rollback does not leave a committed partial checkpoint.
The new table is append-only for the runtime role, with a database digest check
and update/delete rejection trigger. The runtime ubuntu role has SELECT/INSERT;
the existing account administration reader has SELECT. Other roles receive no
new access. No credentials enter this table.

## Migration and release boundaries

The live release is built independently for each role from its actual previous
release, not copied wholesale from the dirty workspace. The allowlist is
v2_core/state.py and v2_core/coverage_evidence.py for compatibility readers, plus
v2_core/account_coverage.py only for the execution controller. SQL migration
20261004_coverage_blobs.sql creates the additive table. QA dummy configuration
is explicitly excluded from deployed directories. No pending strategy changes,
API keys, account switch or LIVE activation are included.

Old current rows are converted in bounded owner-only transactions. Each batch
takes an exclusive state-table lock, proves current and history payloads equal,
and checks namespace, version, reason and deletion flags. Only the current
representation changes. The state-version trigger is disabled and restored
within that locked transaction; errors roll back both data and trigger DDL.
The immutable history payload is never changed. Interrupted runs can enumerate
remaining inline rows and resume without repeating committed conversions.

Physical space requires a separate rewrite of v2_business_state after all
conversions complete. A preflight checks remaining inline rows and rewrite space;
the rewrite has lock/statement timeouts and a low-space cancellation guard.
The much larger immutable history table is not rewritten or truncated.

The maintenance scripts and non-secret diagnostics are retained in
/tmp/v2-storage-recovery.GTjYXd. Storage releases have per-role names beginning
storage-dedup and ending 20261004-v1. Operational scripts are one-time tools,
not an automatic retention policy.

## Safe cleanup already completed

Ten old, individually validated isolated QA PostgreSQL data directories and
three validated offline QA archives were removed, reclaiming approximately
1.4 GiB before migration. Owner, clean shutdown, cluster identity or exact
isolated socket settings, age, keep markers, symlinks and process references
were checked. Logs were retained. These deleted synthetic databases and archives
cannot be directly restored; test fixtures can recreate their test data.
No active database, Redis trading state or business record was deleted.

## Verification

The exact deduplication release candidate and final maintenance helper passed
290 tests with no skips in the normal isolated PostgreSQL and Redis runner after
disk recovery. This includes committed/uncommitted crash-restart recovery. Tests cover
coverage decisions, inventory, admission, portfolio guards, managed recovery,
state idempotency, reference integrity, transaction rollback, account isolation,
content ordering and narrow migration refusal. Ruff and scoped whitespace checks
passed. Preliminary RAM-backed checks passed 289 tests with the crash test
skipped; the final normal-runner test above closes that specific verification
gap. All disposable QA database instances were stopped and their synthetic data
removed; diagnostic logs remain. No test invoked a live trading endpoint.

For a 1,500-order fixture, one changed record adds three objects: the record,
its chunk and its manifest. An unchanged repeated snapshot adds none. A large
real historical sample reconstructed exactly: 4,420,546 bytes of original JSON
became a 9,293-byte observation plus 3,040,381 bytes of initially stored unique
facts. Repeating it added no fact objects. These are serialized-byte comparisons,
not claims about final PostgreSQL disk compression or trading profitability.

## Completed recovery and live observations

All 68,920 old observations were converted. The post-migration join found zero
missing histories or incorrect self-references. The state version and no-delete
triggers were enabled. Physical rewrite completed in 42.9 seconds:

- Current state table: 38,718,959,616 to 431,972,352 bytes, about 36.1 GiB to
  412 MiB. Reclaimed 38,286,987,264 bytes, approximately 35.66 GiB.
- Original history table: 42,268,508,160 bytes before and after the rewrite.
- Database: approximately 89 GiB to 54 GiB. Root filesystem: 68 percent used,
  approximately 37 GiB available at acceptance.
- Before execution resumed, content fingerprints were unchanged for all 1,811
  orders, 567 fills, 207 settlements and 1,560 episodes. These fingerprints
  certify the maintenance interval, not a promise that resumed trading never
  appends new records.

The market hub, execution controller, account administration, dashboard and
reentry observer were active with zero new automatic restarts at acceptance.
The independent disk-health assessment had no active findings. Market heartbeat
age was below six seconds in the final sampled runtime checks.

Recovery did not bypass exchange verification. A protected systemd one-shot
performed signed GET inventory reads with order, protection and exit mutation
permissions disabled. It confirmed no venue positions or ordinary/conditional
orders and a flat local ledger. The controller started in DRAINING, not OPEN.
A fresh second inventory immediately preceded the existing owner API request
to resume the identical SANDBOX account and credential binding. The API's
expected-epoch check and controller's independent preflight remained active.
Route advanced from epoch 34 to ACTIVE epoch 35, with stored policy unchanged.
No LIVE route, forced close or synthetic order was requested.

A deployed new-format audit at 11:14:05 UTC+8 reconstructed successfully:
current pointer 93 bytes, history observation 3,048 stored bytes, complete
before/after facts equal, and ACCOUNT_COVERAGE_CLEAR. Fact counts were 1,519
episodes, 1,743 orders and 188 protection records. At that sample, the shared
fact table contained 3,563 objects and occupied about 3 MiB; facts are reused
rather than recopied into each audit.

At 11:15:58 a second audit again reconstructed equal facts and reported CLEAR.
The fact table remained at 3,563 objects and 3,170,304 bytes; only the new
inventory observation was added, measuring 3,174 stored history bytes.

The observed completed pipeline had entry_dispatch_enabled=true, market CURRENT,
and protection, exits, followups and settlement CLEAR. A natural post-recovery
AINUSDT opening order reached FILLED at 11:12:39 UTC+8. This is operational
recovery evidence, not a profitability assertion or acceptance of unrelated
pending strategy upgrades.

## Rollback and remaining limits

Do not point services at an unmodified pre-repair release after migration.
Retain the compatible history-reference reader and, once packed audits exist,
the packed-fact reader and its table. The original legacy history remains a
recovery source; restoring duplicate inline current payloads would need space
and a separately reviewed migration. Do not disable immutability or delete
history to make an old release start.

Deduplication prevents repeated full-ledger snapshots, not all future growth.
Inventories, audit observations, changing facts, signal receipts and decision
evidence remain legitimate growing data. Historical coverage payloads still
occupy their original immutable history space. Any later archival or retention
policy must preserve reconstructability and have its own restore verification.
The existing disk-health warning and disposable-QA cleanup timers remain active.
Their deployment is SANDBOX-scoped and is not evidence of LIVE readiness.
