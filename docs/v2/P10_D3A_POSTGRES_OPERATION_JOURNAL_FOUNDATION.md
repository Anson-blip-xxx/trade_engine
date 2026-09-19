# P10 D3A - PostgreSQL Operation Journal Foundation

> **IMPLEMENTED / CLOSED as dormant schema/domain foundation.** The schema is
> not applied and no runtime caller, migration, connection, or trading behavior
> is added.

PostgreSQL is selected as authority for operation stage, UNKNOWN recovery,
retry scheduling, leases, and audit. Redis strict authority continues to own
current slot/projection/desired state and Lua CAS; Binance remains physical
position/order truth. The journal stores identity references and evidence, not
a competing copy of Redis authority.

`OperationRecord` freezes operation identity and normalized input, uses an
explicit legal state machine, increments a version on every transition, and
forbids converting SUBMITTING/UNKNOWN to failure without explicit proof that no
exchange effect occurred. Terminal stages cannot advance.

`postgres_operation_journal_schema.sql` defines `trade_operations`, recovery
and slot/request lookup indexes, generation references, JSONB evidence,
retry time, and owner/lease pairing. Applying this schema remains a separate
deployment gate after adapter, CAS, lease, recovery-scan, backup, and rollback
QA are complete.

`request_id` is deliberately not globally unique yet. D1 must approve its
producer and duplicate scope before a scoped uniqueness constraint is added.

**P10 D3A-PG-FOUNDATION PASS.**
