# V2 Operator Decision Center

> **PRODUCT REQUIREMENT APPROVED / DESIGN PLANNED / NOT IMPLEMENTED.** Human
> attention must be requested through Telegram and resolved in an authenticated
> Web Decision Center. This document adds no notification delivery, HTTP route,
> frontend bundle, approval write, or runtime behavior.

## 1. Product objective

Operators should not need to watch logs or remain continuously online. When an
event needs confirmation, the system continues its approved automatic fallback
policy and creates a durable decision item. Telegram attracts attention; the
Web center explains and records the decision.

Neither channel owns trading truth. PostgreSQL owns the durable decision/audit
record, Redis owns current hot authority, and Binance remains physical truth.

## 2. Telegram notification contract

Each notification should contain:

- severity and plain-language title;
- environment, non-secret account alias, symbol, position side, and episode;
- event ID, operation ID, current age, approval deadline, and next automatic
  fallback;
- concise trigger-time versus current-state summary;
- a deep link to the exact Web decision item;
- delivery/idempotency identity so retries are observable and deduplicated.

Telegram must never include credentials, API keys, webhook secrets, raw signed
requests, or a reusable approval token. High-risk approval must not be a
one-click Telegram action. A deep link identifies the item but still requires
normal Web authentication, authorization, revalidation, and confirmation.

Telegram delivery is not business acknowledgement. Delivery failure must not
pause the approved safety fallback. The durable Web inbox remains authoritative
and notification delivery uses a retryable outbox with bounded backoff,
delivery state, and escalation-channel support.

## 3. Web information architecture

```text
+--------------------------------------------------------------------------+
| V2 DECISION CENTER | system health | open halt | clock | operator         |
+----------------------+--------------------------------+------------------+
| Decision inbox       | Incident workspace             | Safe actions     |
| severity / account   | trigger snapshot               | deadline         |
| type / age / status  |        vs current snapshot     | fallback preview |
| filters / search     | field-level diff + timeline    | approve/reject   |
|                      | exchange + Redis + PG evidence | rebuild decision |
+----------------------+--------------------------------+------------------+
| Immutable audit trail | notification delivery | comments | export         |
+--------------------------------------------------------------------------+
```

### Inbox

- severity, age, deadline, acknowledgement, approval, expiry, and fallback
  state;
- filters for environment, account, symbol, event type, owner, and status;
- saved views for unprotected positions, UNKNOWN mutations, external exposure,
  expiring approvals, notification failures, and quarantined work;
- bulk acknowledgement only; never bulk trading approval.

### Incident workspace

- trigger-time snapshot and current snapshot displayed side by side;
- highlighted changes to operation version, episode/generations, revisions,
  position direction/quantity, aliases, statuses, and evidence time;
- lifecycle timeline from detection through retries, notifications, fallback,
  approvals, expiry, and final outcome;
- explicit explanation of why the old event is still current, stale, expired,
  UNKNOWN, or quarantined;
- current Redis/PostgreSQL/Binance evidence provenance and freshness without
  exposing secrets.

### Safe-action panel

- recommended action, alternatives, maximum risk, reversibility, and automatic
  fallback countdown;
- dry-run/revalidation preview before confirmation;
- approve, reject, quarantine, rebuild-from-current-state, and acknowledge as
  separate actions;
- mutation actions require re-authentication, reason text, narrow RBAC, exact
  `ApprovalBinding`, and final current-state revalidation;
- no generic market-order console and no default emergency-close button.

## 4. Visual direction

The intended style is a high-density dark trading/observability console rather
than a generic admin template:

- near-black/navy canvas with layered graphite panels;
- cyan for current verified state, amber for deadlines/UNKNOWN, red for exposed
  risk, violet for operator actions, and green only for confirmed safe state;
- compact monospace identifiers beside readable human labels;
- animated timeline pulses and countdown accents, with motion reduced when the
  OS accessibility preference requests it;
- field-level snapshot diffs, exposure sparklines, lifecycle rails, and service
  health indicators;
- responsive desktop-first layout with a safe read/acknowledge mobile view;
- WCAG-conscious contrast, icons plus text, and no color-only semantics.

Visual polish must not obscure consequence, deadline, provenance, or whether an
action increases risk.

## 5. Security and consistency

- authenticated TLS-only Web access, least-privilege RBAC, and MFA/re-auth for
  mutation approval;
- CSRF protection, secure sessions, rate limits, audit logging, and no secrets
  in browser payloads or URLs;
- short-lived navigation links may be signed, but are never approval grants;
- approval requests are idempotent and bound to operation/version/type, slot,
  episode/generations, authority/desired revisions, policy version, evidence
  time/digest, direction, quantity, action, and exclusive expiry;
- stale or expired pages disable their old action and offer only
  rebuild-from-current-state;
- reconnect/live-update races must never silently replace the snapshot the
  operator reviewed; changed evidence requires a visible new decision version.

## 6. Delivery slices

1. durable decision inbox schema and notification outbox;
2. read-only authenticated inbox/detail API;
3. Telegram renderer, bounded delivery retry, and safe deep links;
4. read-only dark UI with snapshot diff, timeline, health, and countdown;
5. acknowledgement/comment/audit workflow;
6. approval signing with RBAC, re-auth, expiry, and server-side revalidation;
7. default-off executor integration, failure injection, and operator drill.

The read-only center should ship before any mutation button. A Telegram outage,
Web outage, or absent operator never overrides the autonomous delayed-event
safety policy.

**V2-OPERATOR-DECISION-CENTER PRODUCT NOTE PASS.**
