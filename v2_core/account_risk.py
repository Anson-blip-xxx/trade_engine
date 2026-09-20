"""PG-atomic reference-notional admission budgets, not exchange margin estimates.

Lock order: intent root -> account advisory lock -> account row -> reservation.
No network calls under locks. Unknown outcomes never release capacity by timeout.
All numeric amounts are decimal strings; one quote currency per account scope.
"""

from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, Decimal, localcontext
from uuid import UUID

from psycopg.types.json import Jsonb

from v2_core.evidence import canonical
from v2_core.ingress import identity, milliseconds, symbol
from v2_core.ledger import amount, emit


class AccountRiskDenied(ValueError):
    """Fixed rejection code; no raw configuration or transport exceptions."""

    def __init__(self, code, evidence=None):
        super().__init__(code)
        self.evidence = {} if evidence is None else evidence


@dataclass(frozen=True)
class AccountScope:
    exchange: str
    account_id: str
    environment: str
    product: str

    def __post_init__(self):
        identity(self.account_id)
        if (
            self.exchange != "BINANCE"
            or self.product != "FUTURES"
            or self.environment not in {"SANDBOX", "LIVE"}
        ):
            raise ValueError("explicit linear futures account scope required")

    @property
    def key(self):
        return canonical(asdict(self))


@dataclass(frozen=True)
class AccountPolicy:
    currency: str
    max_notional: str
    max_positions: int
    cooldown_ms: int
    reference_source: str
    max_reference_age_ms: int

    def __post_init__(self):
        amount(self.max_notional, positive=True)
        identity(self.reference_source)
        if self.currency not in {"USDT", "USDC"}:
            raise ValueError("explicit quote currency required")
        for value, lower, upper in (
            (self.max_positions, 1, 10000),
            (self.cooldown_ms, 0, 86400000),
            (self.max_reference_age_ms, 1, 3600000),
        ):
            if type(value) is not int or not lower <= value <= upper:
                raise ValueError("bounded risk policy required")


@dataclass(frozen=True)
class RiskReference:
    environment: str
    symbol: str
    source: str
    price: str
    observed_at_ms: int

    def __post_init__(self):
        symbol(self.symbol)
        identity(self.source)
        amount(self.price, positive=True)
        milliseconds(self.observed_at_ms)
        if self.environment not in {"SANDBOX", "LIVE"}:
            raise ValueError("explicit reference environment required")


def _lock(conn, scope):
    # Even an unconfigured scope locks here, serializing first policy activation
    # with all Orders.transition submission paths (including compatibility ports).
    conn.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
        ("account-risk:" + scope,),
    )


def _usage(conn, scope):
    return conn.execute(
        "SELECT COALESCE(sum(notional),0),count(*) FROM v2_risk_reservations WHERE scope=%s AND status='HELD'",
        (scope,),
    ).fetchone()


class AccountRisk:
    def __init__(self, connect):
        self._connect = connect

    def configure(self, scope, policy, *, expected_version):
        """Explicit operator configuration with CAS; never called automatically."""
        if not isinstance(scope, AccountScope) or not isinstance(policy, AccountPolicy):
            raise TypeError("typed scope and policy required")
        if type(expected_version) is not int or expected_version < 0:
            raise ValueError("policy version required")
        config = asdict(policy)
        with self._connect() as conn:
            _lock(conn, scope.key)
            current = conn.execute(
                "SELECT a.version,p.config FROM v2_risk_accounts a JOIN v2_risk_policies p USING(scope,version) WHERE a.scope=%s FOR UPDATE OF a",
                (scope.key,),
            ).fetchone()
            if current and current == (expected_version + 1, config):
                return current[0]
            if (0 if current is None else current[0]) != expected_version:
                raise ValueError("risk policy version conflict")
            if current is None:
                active = conn.execute(
                    """SELECT 1 FROM v2_episodes e JOIN v2_trade_intents i ON i.intent_id=e.episode_id
                    WHERE (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                      AND e.status='ACTIVE' LIMIT 1""",
                    (
                        scope.exchange,
                        scope.account_id,
                        scope.environment,
                        scope.product,
                    ),
                ).fetchone()
                if active:
                    raise ValueError(
                        "cannot activate risk over unreserved active episodes"
                    )
                conn.execute(
                    "INSERT INTO v2_risk_accounts(scope,version) VALUES (%s,1)",
                    (scope.key,),
                )
            elif current[1]["currency"] != policy.currency:
                raise ValueError("account risk currency cannot change")
            version = expected_version + 1
            conn.execute(
                "INSERT INTO v2_risk_policies(scope,version,config) VALUES (%s,%s,%s)",
                (scope.key, version, Jsonb(config)),
            )
            conn.execute(
                "UPDATE v2_risk_accounts SET version=%s WHERE scope=%s",
                (version, scope.key),
            )
        return version

    def usage(self, scope):
        if not isinstance(scope, AccountScope):
            raise TypeError("typed scope required")
        with self._connect() as conn:
            _lock(conn, scope.key)
            row = conn.execute(
                "SELECT version FROM v2_risk_accounts WHERE scope=%s", (scope.key,)
            ).fetchone()
            if row is None:
                raise ValueError("risk policy missing")
            notional, positions = _usage(conn, scope.key)
        return {
            "policy_version": row[0],
            "notional": str(notional),
            "positions": positions,
        }

    def sweep_releases(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("bounded release scan required")
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT r.episode_id::text FROM v2_risk_reservations r
                JOIN v2_episodes e USING(episode_id)
                WHERE r.status='HELD' AND e.status IN ('ABORTED','SETTLED')
                ORDER BY r.created_at,r.episode_id LIMIT %s""",
                (limit,),
            ).fetchall()
        released = 0
        for (episode_id,) in rows:
            with self._connect() as conn:
                conn.execute(
                    "SELECT 1 FROM v2_trade_intents WHERE intent_id=%s FOR UPDATE",
                    (episode_id,),
                )
                released += release_terminal(conn, episode_id)
        return released


def reserve_open(conn, order_id, *, reference=None, required=False):
    """Called only inside the root-locked submission CAS transaction."""
    row = conn.execute(
        """SELECT i.intent_id::text,i.exchange,i.account_id,i.environment,i.product,
        i.payload,e.snapshot FROM v2_orders o JOIN v2_trade_intents i ON i.intent_id=o.episode_id
        JOIN v2_decision_evidence e USING(evidence_ref) WHERE o.order_id=%s""",
        (order_id,),
    ).fetchone()
    # Unsupported legacy contracts may exist in isolated core QA; they cannot
    # opt into this risk model or bypass a configured supported scope.
    if row[1] != "BINANCE" or row[4] != "FUTURES" or row[3] not in {"SANDBOX", "LIVE"}:
        if required:
            raise AccountRiskDenied("UNSUPPORTED_RISK_SCOPE")
        return None
    scope = AccountScope(*row[1:5]).key
    _lock(conn, scope)
    config = conn.execute(
        """SELECT a.version,p.config,a.last_reserved_at FROM v2_risk_accounts a
        JOIN v2_risk_policies p USING(scope,version) WHERE a.scope=%s FOR UPDATE OF a""",
        (scope,),
    ).fetchone()
    if config is None:
        if required:
            raise AccountRiskDenied("RISK_POLICY_MISSING")
        return None
    version, policy, last = config

    def deny(code):
        return AccountRiskDenied(
            code, {"scope": scope, "policy_version": version, "policy": policy}
        )

    if not isinstance(reference, RiskReference):
        raise deny("RISK_REFERENCE_REQUIRED")
    if (
        reference.environment != row[3]
        or reference.symbol != row[5]["symbol"]
        or reference.source != policy["reference_source"]
        or not reference.symbol.endswith(policy["currency"])
    ):
        raise deny("RISK_REFERENCE_SCOPE_MISMATCH")
    now, cooldown_ready = conn.execute(
        """SELECT floor(extract(epoch FROM clock_timestamp())*1000)::bigint,
        %s::timestamptz IS NULL OR clock_timestamp()>=%s::timestamptz+%s*interval '1 millisecond'""",
        (last, last, policy["cooldown_ms"]),
    ).fetchone()
    if not 0 <= now - reference.observed_at_ms < policy["max_reference_age_ms"]:
        raise deny("RISK_REFERENCE_STALE")
    snapshot = row[6]
    if (
        type(snapshot.get("expires_at_ms")) is not int
        or not snapshot["observed_at"]
        <= snapshot["decided_at"]
        <= now
        < snapshot["expires_at_ms"]
    ):
        raise deny("RISK_DECISION_EXPIRED")
    existing = conn.execute(
        "SELECT status,notional,reference,policy_version FROM v2_risk_reservations WHERE episode_id=%s FOR UPDATE",
        (row[0],),
    ).fetchone()
    used, positions = _usage(conn, scope)
    if existing:
        if existing[0] != "HELD":
            raise deny("RISK_RESERVATION_RELEASED")
        if amount(reference.price, positive=True) > amount(
            existing[2]["price"], positive=True
        ):
            raise deny("RISK_REFERENCE_EXCEEDS_RESERVATION")
        if (
            used > amount(policy["max_notional"], positive=True)
            or positions > policy["max_positions"]
        ):
            raise deny("RISK_ACCOUNT_OVER_LIMIT")
        return {
            "policy_version": version,
            "reservation_policy_version": existing[3],
            "notional": str(existing[1]),
            "currency": policy["currency"],
            "reference": asdict(reference),
        }
    if not cooldown_ready:
        raise deny("RISK_COOLDOWN")
    with localcontext() as ctx:
        ctx.prec = 80
        notional = (
            amount(row[5]["quantity"], positive=True)
            * amount(reference.price, positive=True)
        ).quantize(Decimal("1e-18"), rounding=ROUND_CEILING)
        if notional >= Decimal("1e20") or used + notional > amount(
            policy["max_notional"], positive=True
        ):
            raise deny("RISK_NOTIONAL_LIMIT")
    if positions >= policy["max_positions"]:
        raise deny("RISK_POSITION_LIMIT")
    conn.execute(
        """INSERT INTO v2_risk_reservations(episode_id,scope,policy_version,notional,reference)
        VALUES (%s,%s,%s,%s,%s)""",
        (row[0], scope, version, notional, Jsonb(asdict(reference))),
    )
    conn.execute(
        "UPDATE v2_risk_accounts SET last_reserved_at=clock_timestamp() WHERE scope=%s",
        (scope,),
    )
    proof = {
        "policy_version": version,
        "reservation_policy_version": version,
        "notional": str(notional),
        "currency": policy["currency"],
        "reference": asdict(reference),
    }
    emit(conn, row[0], "ACCOUNT_RISK_RESERVED", proof)
    return proof


def release_terminal(conn, episode_id):
    """Caller holds the intent root; no operator-provided flat/release flag."""
    episode_id = str(UUID(episode_id))
    row = conn.execute(
        """SELECT r.scope,e.status,e.accounting_revision FROM v2_risk_reservations r
        JOIN v2_episodes e USING(episode_id) WHERE r.episode_id=%s AND r.status='HELD'""",
        (episode_id,),
    ).fetchone()
    if row is None or row[1] not in {"ABORTED", "SETTLED"}:
        return False
    _lock(conn, row[0])
    nonterminal = conn.execute(
        "SELECT 1 FROM v2_orders WHERE episode_id=%s AND status NOT IN ('FILLED','CANCELLED','REJECTED') LIMIT 1",
        (episode_id,),
    ).fetchone()
    totals = dict(
        conn.execute(
            """SELECT o.leg,sum(f.quantity) FROM v2_orders o JOIN v2_fills f USING(order_id)
        WHERE o.episode_id=%s GROUP BY o.leg""",
            (episode_id,),
        ).fetchall()
    )
    if nonterminal or (row[1] == "ABORTED" and totals):
        return False
    if row[1] == "SETTLED":
        if totals.get("OPEN", 0) <= 0 or totals.get("OPEN") != totals.get("CLOSE"):
            return False
        if (
            conn.execute(
                "SELECT 1 FROM v2_settlements WHERE episode_id=%s AND revision=%s",
                (episode_id, row[2]),
            ).fetchone()
            is None
        ):
            return False
    changed = conn.execute(
        """UPDATE v2_risk_reservations SET status='RELEASED',released_at=clock_timestamp(),release_reason=%s
        WHERE episode_id=%s AND status='HELD' RETURNING episode_id""",
        (row[1], episode_id),
    ).fetchone()
    if changed:
        emit(
            conn,
            episode_id,
            "ACCOUNT_RISK_RELEASED",
            {"reason": row[1], "accounting_revision": row[2]},
        )
    return changed is not None
