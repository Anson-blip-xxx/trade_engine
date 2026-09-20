"""Account drawdown state from ordered, explicit balance observations in PG.

No Redis/file authority and no venue I/O. This is a balance-based legacy policy,
not equity/margin risk: deposits and unrealized PnL need a separate reconciler.
"""

import json
import re
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical
from v2_core.ingress import identity, milliseconds
from v2_core.ledger import amount
from v2_core.state import BusinessState, StateKey, StateSnapshot

RECOVERY_DELAY_MS = 4 * 3600000
RETRY_DELAY_MS = 6 * 3600000


@dataclass(frozen=True)
class BalanceObservation:
    observation_id: str
    balance: str
    observed_at_ms: int
    evidence_digest: str

    def __post_init__(self):
        identity(self.observation_id)
        if amount(self.balance) < 0:
            raise ValueError("nonnegative balance required")
        milliseconds(self.observed_at_ms)
        if not isinstance(self.evidence_digest, str) or not re.fullmatch(
            r"[0-9a-f]{64}", self.evidence_digest
        ):
            raise ValueError("balance evidence digest required")


def advance(previous, *, balance, observed_at_ms):
    """Pure legacy 8%/15% drawdown policy, with zero balance fail-closed.

    Timers only advance with a new source observation, never with a replay or
    polling clock. Peak uses exact decimals; threshold tests avoid division.
    """
    bal = amount(balance)
    if bal < 0:
        raise ValueError("negative balance")
    milliseconds(observed_at_ms)
    if previous is not None and observed_at_ms <= previous["observed_at_ms"]:
        raise ValueError("balance observations must advance source time")
    peak = max(bal, amount(previous["peak_balance"]) if previous else Decimal(0))
    state = {
        "observed_at_ms": observed_at_ms,
        "peak_balance": format(peak, "f"),
        "pause_at_ms": None,
        "base_balance": None,
        "loss_locked": False,
        "factor": "1",
        "mode": "normal",
    }
    with localcontext() as ctx:
        ctx.prec = 100
        if bal == 0 or peak - bal >= peak * Decimal(".15"):
            paused = previous is not None and previous["pause_at_ms"] is not None
            if paused:
                for key in ("pause_at_ms", "base_balance", "loss_locked"):
                    state[key] = previous[key]
            else:
                state.update(pause_at_ms=observed_at_ms, base_balance=balance)
            if (
                paused
                and not state["loss_locked"]
                and bal <= amount(state["base_balance"]) * Decimal(".98")
            ):
                state.update(
                    loss_locked=True, pause_at_ms=observed_at_ms, base_balance=balance
                )
            state.update(factor="0", mode="halt")
            if state["loss_locked"]:
                if observed_at_ms - state["pause_at_ms"] >= RETRY_DELAY_MS:
                    state.update(
                        loss_locked=False,
                        pause_at_ms=observed_at_ms,
                        base_balance=balance,
                    )
            elif bal > 0 and observed_at_ms - state["pause_at_ms"] >= RECOVERY_DELAY_MS:
                state.update(factor="0.25", mode="recovery")
        elif peak - bal >= peak * Decimal(".08"):
            state.update(factor="0.5", mode="reduced")
    return state


class DrawdownState:
    """Serial, idempotent observations plus immutable BusinessState history.

    record() returns the observation's historical state, NOT a current permit.
    current() checks PG time again. Submission-time checks still must be wired
    into the order transaction; calling current() beforehand is not atomic risk.
    """

    def __init__(self, connect, scope, *, source, currency, max_age_ms):
        if not isinstance(scope, AccountScope):
            raise TypeError("typed account scope required")
        identity(source)
        if (
            currency not in {"USDT", "USDC"}
            or type(max_age_ms) is not int
            or not 1 <= max_age_ms <= 3600000
        ):
            raise ValueError("bounded currency/freshness configuration required")
        self._connect = connect
        self.key = StateKey(
            **asdict(scope), namespace="account-drawdown", key="balance-v1"
        )
        self.config = {
            "source": source,
            "currency": currency,
            "max_age_ms": max_age_ms,
            "policy": "legacy-balance-drawdown-v1",
        }

    def _verify(self, snapshot):
        if snapshot.deleted:
            raise ValueError(
                "drawdown state tombstoned; explicit reconciliation required"
            )
        payload = json.loads(snapshot.payload_json)
        if payload["config"] != self.config:
            raise ValueError("drawdown configuration conflict")
        return payload

    def _fresh(self, conn, observation):
        now = conn.execute(
            "SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint"
        ).fetchone()[0]
        if not 0 <= now - observation["observed_at_ms"] <= self.config["max_age_ms"]:
            raise ValueError("balance observation stale or future")

    def record(self, observation):
        if not isinstance(observation, BalanceObservation):
            raise TypeError("typed balance observation required")
        incoming = asdict(observation)
        with self._connect() as conn:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("drawdown:" + self.key.identity,),
            )
            state = BusinessState(lambda: nullcontext(conn))
            current = state.read(self.key)
            payload = self._verify(current) if current else None
            old = conn.execute(
                "SELECT version,payload,deleted FROM v2_state_history WHERE state_id=%s AND request_key=%s",
                (self.key.identity, observation.observation_id),
            ).fetchone()
            if old:
                if old[1]["observation"] != incoming or old[1]["config"] != self.config:
                    raise ValueError("balance observation identity conflict")
                return StateSnapshot(old[0], canonical(old[1]), old[2])
            self._fresh(conn, incoming)
            updated = {
                "config": self.config,
                "observation": incoming,
                "state": advance(
                    None if payload is None else payload["state"],
                    balance=observation.balance,
                    observed_at_ms=observation.observed_at_ms,
                ),
            }
            result = state.change(
                self.key,
                expected_version=current.version if current else 0,
                request_key=observation.observation_id,
                payload=updated,
                reason="balance observation",
            )
            if result.code != "APPLIED":
                raise ValueError("drawdown state concurrently changed")
            return StateSnapshot(result.version, canonical(updated), False)

    def current(self):
        with self._connect() as conn:
            snapshot = BusinessState(lambda: nullcontext(conn)).read(self.key)
            if snapshot is None:
                raise ValueError("balance observation unavailable")
            payload = self._verify(snapshot)
            self._fresh(conn, payload["observation"])
            return snapshot
