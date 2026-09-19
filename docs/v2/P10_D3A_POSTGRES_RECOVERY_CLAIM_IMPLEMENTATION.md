# P10 D3A - PostgreSQL Bounded Recovery Claim

> **IMPLEMENTED / CLOSED as a dormant persistence primitive.** No startup
> scanner, scheduler, recovery worker, service wiring, or schema deployment is
> activated.

`claim_recovery_batch` atomically discovers and leases a bounded set of due,
non-terminal, unowned-or-expired operations. It returns `CLAIMED`, `EMPTY`, or
write-ambiguous `UNKNOWN`; an exception after commit is never reported as an
empty queue.

The SQL applies five concurrency controls:

1. `next_attempt_at` and lease expiry are evaluated with the PostgreSQL clock;
2. `row_number() PARTITION BY slot_digest` selects only the oldest due
   operation per exchange slot;
3. an active lease on any other non-terminal operation blocks the whole slot;
4. `FOR UPDATE ... SKIP LOCKED` lets workers claim disjoint rows without
   waiting on the same operation;
5. a transaction advisory lock derived from `slot_digest` closes the race
   between different operation rows for the same slot. Direct single-operation
   claim uses the same advisory lock and active-slot guard.

Every claimed row receives one owner, one database-clock expiry, and exactly
one version increment in the same transaction. The API caps batch size at
1000 and returns records in deterministic due order.

Opt-in tests against an ephemeral UTF-8 PostgreSQL 16 cluster verify the real
schema and SQL, one-per-slot selection, direct-claim exclusion, and two
concurrent recovery workers claiming four disjoint slots without duplicates.

Remaining work is directive-specific execution ports, bounded scheduler policy,
exchange reconciliation adapters, fault injection, and default-off runtime
activation gates.

**P10 D3A-PG-RECOVERY-CLAIM PASS.**
