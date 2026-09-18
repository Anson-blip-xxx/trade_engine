# P10-07B / P10-D5A-1 - Slot Namespace And Principal Resolver

> First Phase 10 production implementation ticket. It adds a dormant pure leaf
> module only. **ACTIVE RUNTIME BEHAVIOR = 0 CHANGE.**

## 1. Scope

P10-07B implements identity primitives required by the future episode-authority
store:

- explicit `AccountPrincipal` value object and resolver;
- exchange/product/environment/mode/slot-side normalization;
- immutable `ExchangePositionKey`;
- deterministic canonical and storage serialization;
- validation and import-isolation tests.

It does not wire identity into open, close, state, Redis, protection, worker,
reconcile, marker, startup, or configuration loading.

## 2. New Leaf Package

```text
position_identity/
  __init__.py
  principal.py
  slot.py
```

Dependency direction is stdlib-only except imports inside the same leaf package.
There are no imports from PositionManager, execution, lifecycle, protection,
reconcile, state, Redis, PostgreSQL, requests, or strategy modules.

Importing `position_identity` performs no file, environment, network, Redis,
database, thread, worker, or exchange activity.

## 3. Principal Contract

```text
AccountPrincipal(account_principal_id: str)
resolve_account_principal(value)
resolve_account_principal_from_config(mapping)
ACCOUNT_PRINCIPAL_CONFIG_KEY = ACCOUNT_PRINCIPAL_ID
```

Rules:

- alias must be supplied explicitly;
- surrounding whitespace is removed;
- empty, missing, non-string, or control-character values raise `ValueError`;
- no default, `main`, `unknown`, random, credential hash, wallet inference, or
  exchange query fallback exists;
- resolver accepts a string or an already-loaded generic mapping only;
- resolver does not read files or environment variables.

Existing deployment configuration is unaffected because no active code calls
the resolver.

## 4. Security And Rotation

Principal APIs contain no API-key, API-secret, private-key, credential object,
wallet, or account-query input. Authentication material is not accepted and
cannot enter canonical serialization through the typed schema.

Credential rotation leaves identity unchanged because credentials are not part
of `AccountPrincipal` or `ExchangePositionKey`. Operators retain the same
logical alias while rotating keys.

The implementation does not attempt heuristic secret scanning. Its API avoids
accepting credentials in the first place.

## 5. Canonical Enums And Normalization

| Domain | Canonical values | Accepted compatibility aliases |
|---|---|---|
| exchange | `BINANCE` | case/whitespace normalized |
| product | `FUTURES` | `USD_M`, `USD_M_FUTURES` |
| environment | `PROD`, `DEMO`, `SANDBOX` | production/mainnet, test/testnet, paper |
| position mode | `ONE_WAY`, `HEDGE` | one-way/oneway, dual-side |
| slot side | `BOTH`, `LONG`, `SHORT` | case/whitespace normalized |

HEDGE is representable for forward compatibility but is not enabled in any
runtime path. Current repository profile remains `BINANCE/FUTURES/ONE_WAY/BOTH`.

## 6. Slot-Side Semantics

`slot_side_for_strategy(mode, strategy_side)` separates strategy direction from
physical exchange slot identity:

```text
ONE_WAY + LONG  -> BOTH
ONE_WAY + SHORT -> BOTH
HEDGE + LONG    -> LONG
HEDGE + SHORT   -> SHORT
```

The `ExchangePositionKey` constructor itself enforces valid combinations:

- ONE_WAY requires BOTH;
- HEDGE requires LONG or SHORT.

No trading behavior is enabled by representing HEDGE.

## 7. ExchangePositionKey

Immutable fields:

```text
exchange
product
environment
account_principal_id
position_mode
symbol
slot_side
```

`ExchangePositionKey.one_way(...)` constructs the current physical slot profile.
Symbol normalization is minimal: trim and uppercase; empty, whitespace-containing,
or control-character values are rejected. No exchange-symbol mapping was added.

The value object is frozen, equality-stable, and hashable. `system`, strategy
side, legacy `position_id`, request ID, order ID, fill ID, and credentials are
not fields and cannot alter slot identity.

## 8. Deterministic Serialization

Two representations are supplied:

```text
to_canonical_string()
  canonical sorted compact JSON with ASCII escaping

to_storage_key()
  pm:slot:v1:<sha256(canonical-string)>
```

Canonical JSON is length-safe and unambiguous for principal values containing
delimiters, spaces, slashes, colons, or Unicode. Storage keys contain only the
fixed prefix and lowercase hexadecimal digest, making them Redis-key safe.

`repr`, object identity, timestamp, random UUID, and Python's randomized string
hash are not used for durable serialization. `__hash__` is deterministic from
the same SHA-256 bytes, while `to_storage_key()` remains the persistence API.

## 9. Isolation Properties

Tests prove:

- identical values serialize identically across clean subprocesses;
- PROD, DEMO, and SANDBOX create different identities;
- two principal aliases create different identities for the same symbol;
- LONG and SHORT strategies converge to ONE_WAY/BOTH;
- system, `position_id`, and request ID cannot affect identity;
- credential rotation fields do not exist in the schema;
- special principal characters cannot collide through delimiters;
- package import loads no PM, Redis, requests, PostgreSQL, lifecycle, or
  protection modules.

## 10. Dormant Wiring Proof

Architecture guards scan all production Python files outside `position_identity`
and assert there is no import or call of the new package.

They also freeze:

- active startup does not require `ACCOUNT_PRINCIPAL_ID`;
- `pm:positions` schema has no episode authority fields;
- `_ALGO_QUEUE` remains the four-tuple;
- worker remains the four-field consumer;
- `redis_store` has no `pm:slot:v1` authority key or CAS adapter;
- no existing business production file changed.

Therefore old deployments start and trade exactly as before even without the new
configuration field.

## 11. Future D5A-2 Integration

P10-07C / D5A-2 will consume these primitives in a dedicated, initially dormant
slot-authority adapter:

```text
AccountPrincipal + ExchangePositionKey
-> dedicated Redis authority key
-> Lua compare-and-transition
-> generation high-water and typed acknowledgement
```

P10-07C must not embed authority in `pm:positions` or use file fallback. Active
flow wiring remains a later ticket after D5A-2 and D5A-3.

## 12. Rollback

P10-07B is one isolated commit. `git revert <P10-07B commit>` removes the leaf
package, tests, and documentation. Since no runtime caller or config requirement
exists, rollback changes no persisted state, queue item, exchange effect, or
trading behavior.

## 13. Verification

| Check | Result |
|---|---|
| dedicated unit tests | PASS |
| clean subprocess import | PASS |
| active runtime caller | none |
| existing production files modified | none |
| Redis authority/state schema | unchanged |
| queue/worker | unchanged |
| behavior suites | PASS |
| diff check | PASS |

**P10-07B PASS.**

P10-D5A-1 is **IMPLEMENTED / CLOSED**. P10-07C / D5A-2 is next and
`READY_FOR_IMPLEMENTATION`.
