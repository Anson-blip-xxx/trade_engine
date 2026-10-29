"""Bounded, account-isolated settlement research; never authorizes an order.

Initial R is net settled PnL divided by actual opening quantity times the
distance from actual average fill to the original decision's stop. Fees and
funding belong in the numerator, not an invented winning-trade estimate.
Unknown historical facts are excluded, never interpreted as neutral signals.
"""

import json
import logging
from collections import Counter
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal, InvalidOperation
from uuid import UUID

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.precision import decimal_text, money_context

_LOG = logging.getLogger(__name__)
_DEFAULTS = {
    "history.mode": "OFF",
    "history.lookback_days": 14,
    "history.min_samples": 30,
    "history.prior_weight": 20,
    "history.soft_factor": "0.75",
    "history.max_samples": 500,
}
_COLUMNS = (
    "episode_id",
    "exchange",
    "account_id",
    "environment",
    "product",
    "producer",
    "symbol",
    "closed_at_ms",
    "settlement_revision",
    "net_pnl",
    "outcome_evidence",
    "recorded_at_ms",
    "settled_at_ms",
    "currency",
    "settlement_evidence",
    "evidence_ref",
    "source",
    "plan",
    "regime",
    "stop_price",
    "decided_at_ms",
    "opened_quantity",
    "closed_quantity",
    "opening_notional",
    "latest_fill_ms",
    "latest_fill_known_at_ms",
    "maintenance",
)
_SQL = """
WITH recent AS MATERIALIZED (
    SELECT * FROM v2_directional_outcomes
    WHERE (exchange,account_id,environment,product)=(%s,%s,%s,%s)
      AND producer=%s AND closed_at_ms BETWEEN %s AND %s
      AND recorded_at <= to_timestamp(%s::numeric/1000)
    ORDER BY closed_at_ms DESC,episode_id DESC LIMIT %s
)
SELECT r.episode_id::text,r.exchange,r.account_id,r.environment,r.product,
    r.producer,r.symbol,r.closed_at_ms,r.settlement_revision,r.net_pnl,
    r.evidence,(extract(epoch FROM r.recorded_at)*1000)::bigint,
    (extract(epoch FROM s.settled_at)*1000)::bigint,s.currency,s.evidence,
    i.evidence_ref,e.snapshot->>'source',
    e.snapshot#>'{features,evaluation,market_plan}',
    e.snapshot#>>'{features,context,directional,regime}',
    e.snapshot#>>'{features,evaluation,sizing,stop_price}',
    e.snapshot->>'decided_at',f.opened_quantity,f.closed_quantity,
    f.opening_notional,f.latest_fill_ms,f.latest_fill_known_at_ms,
    EXISTS(SELECT 1 FROM v2_orders m WHERE m.episode_id=r.episode_id
        AND m.request_evidence->>'origin'='APPROVED_TESTNET_MAINTENANCE')
FROM recent r
LEFT JOIN v2_settlements s ON s.episode_id=r.episode_id
    AND s.revision=r.settlement_revision
LEFT JOIN v2_trade_intents i ON i.intent_id=r.episode_id
    AND (i.exchange,i.account_id,i.environment,i.product,i.producer)=
        (r.exchange,r.account_id,r.environment,r.product,r.producer)
    AND i.status='FILLED'
    AND EXISTS(SELECT 1 FROM v2_episodes ep WHERE ep.episode_id=i.intent_id
        AND ep.status='SETTLED')
LEFT JOIN v2_decision_evidence e ON e.evidence_ref=i.evidence_ref
LEFT JOIN LATERAL (
    SELECT sum(f.quantity) FILTER(WHERE o.leg='OPEN') AS opened_quantity,
        sum(f.quantity) FILTER(WHERE o.leg='CLOSE') AS closed_quantity,
        sum(f.quantity*f.price) FILTER(WHERE o.leg='OPEN') AS opening_notional,
        max(f.occurred_at_ms) AS latest_fill_ms,
        (extract(epoch FROM max(greatest(f.created_at,o.updated_at)))*1000)::bigint
            AS latest_fill_known_at_ms
    FROM v2_orders o JOIN v2_fills f USING(order_id)
    WHERE o.episode_id=r.episode_id
) f ON true
ORDER BY r.closed_at_ms DESC,r.episode_id DESC
"""


@dataclass(frozen=True)
class LayeredHistoryEvidence:
    evidence_json: str
    digest: str
    suggested_factor: str
    applied_factor: str
    status: str

    def as_dict(self):
        return {**json.loads(self.evidence_json), "digest": self.digest}


def _decimal(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, Decimal, int)):
        raise TypeError("INVALID_HISTORICAL_AMOUNT")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("INVALID_HISTORICAL_AMOUNT") from exc
    if not result.is_finite() or abs(result) >= Decimal("1e20"):
        raise ValueError("INVALID_HISTORICAL_AMOUNT")
    if positive and result <= 0:
        raise ValueError("INVALID_HISTORICAL_AMOUNT")
    return result


def _text(value):
    # Reporting precision only: source money is retained separately in proof.
    with money_context():
        return decimal_text(value.quantize(Decimal("1e-18")))


def _controls(controls, scope):
    from v2_core.entry_controls import resolve

    if controls is None:
        values, version = resolve({}), 0
        control_digest = digest(
            canonical({"scope": asdict(scope), "values": values, "version": 0})
        )
    elif isinstance(controls, dict):
        values, version = controls["values"], controls["version"]
        control_digest = controls["digest"]
    else:
        values, version, control_digest = (
            controls.values,
            controls.version,
            controls.digest,
        )
    if resolve(values) != values or control_digest != digest(
        canonical({"scope": asdict(scope), "version": version, "values": values})
    ):
        raise ValueError("LAYERED_HISTORY_CONTROL_SCOPE_MISMATCH")
    settings = {key: values[key] for key in _DEFAULTS}
    if settings["history.mode"] not in {"OFF", "SHADOW", "SOFT"}:
        raise ValueError("INVALID_LAYERED_HISTORY_MODE")
    for key, lower, upper in (
        ("history.lookback_days", 1, 365),
        ("history.min_samples", 1, 500),
        ("history.prior_weight", 0, 10000),
        ("history.max_samples", 1, 1000),
    ):
        value = settings[key]
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError("INVALID_LAYERED_HISTORY_LIMIT")
    if settings["history.min_samples"] > settings["history.max_samples"]:
        raise ValueError("INVALID_LAYERED_HISTORY_LIMIT")
    factor = _decimal(settings["history.soft_factor"], positive=True)
    if factor > 1 or type(version) is not int or version < 0:
        raise ValueError("INVALID_LAYERED_HISTORY_FACTOR")
    if not isinstance(control_digest, str) or len(control_digest) != 64:
        raise ValueError("INVALID_LAYERED_HISTORY_CONTROL_DIGEST")
    return settings, version, control_digest


def _family(plan, source, regime):
    plan = asdict(plan) if is_dataclass(plan) else plan
    fields = {
        "strategy": plan.get("strategy"),
        "source": source,
        "side": plan.get("side"),
        "entry_mode": plan.get("entry_mode"),
        "regime": regime,
    }
    if any(not isinstance(v, str) or not v or len(v) > 80 for v in fields.values()):
        raise ValueError("HISTORICAL_FAMILY_UNKNOWN")
    if fields["strategy"] not in {"S6", "S8"} or fields["side"] not in {
        "LONG",
        "SHORT",
    }:
        raise ValueError("HISTORICAL_FAMILY_UNKNOWN")
    return fields


def _binding(scope, producer, signal, context, plan, asof_ms):
    if not isinstance(scope, AccountScope) or producer not in {"s6", "s8"}:
        raise ValueError("SCOPED_DIRECTIONAL_HISTORY_REQUIRED")
    if (
        context["account_scope"] != asdict(scope)
        or context["environment"] != scope.environment
        or context["symbol"] != signal["symbol"]
        or type(asof_ms) is not int
        or asof_ms != context["assembled_at"]
    ):
        raise ValueError("LAYERED_HISTORY_CONTEXT_MISMATCH")
    family = _family(plan, signal["source"], context["directional"]["regime"])
    if family["strategy"].lower() != producer:
        raise ValueError("LAYERED_HISTORY_PRODUCER_MISMATCH")
    return {
        "account_scope": asdict(scope),
        "symbol": signal["symbol"],
        "family": family,
        "asof_ms": asof_ms,
    }


def _freeze(proof):
    encoded = canonical(proof)
    return LayeredHistoryEvidence(
        encoded,
        digest(encoded),
        proof["suggested_factor"],
        proof["applied_factor"],
        proof["status"],
    )


def _sample(row, scope, producer, family, *, start_ms, asof_ms):
    if not isinstance(row, dict):
        return None, "MISSING_OR_INVALID_HISTORY_FACT"
    if any(row.get(k) != v for k, v in asdict(scope).items()):
        return None, "SCOPE_MISMATCH"
    if row.get("producer") != producer:
        return None, "STRATEGY_MISMATCH"
    origin = row.get("outcome_evidence")
    if not isinstance(origin, dict):
        return None, "MISSING_OR_INVALID_HISTORY_FACT"
    if (
        row.get("maintenance") is not False
        or origin.get("strategy_learning_eligible") is False
    ):
        return None, "MAINTENANCE_OR_UNKNOWN_ORIGIN"
    try:
        UUID(row["episode_id"])
        if not isinstance(row["evidence_ref"], str) or len(row["evidence_ref"]) != 64:
            return None, "MISSING_OR_INVALID_HISTORY_FACT"
        for key in (
            "closed_at_ms",
            "settled_at_ms",
            "recorded_at_ms",
            "latest_fill_ms",
            "latest_fill_known_at_ms",
        ):
            if type(row[key]) is not int or not 0 <= row[key] <= asof_ms:
                return None, "FUTURE_OR_UNTIMED_FACT"
        if not start_ms <= row["closed_at_ms"]:
            return None, "OUTSIDE_LOOKBACK"
        if not 0 <= int(row["decided_at_ms"]) <= row["closed_at_ms"]:
            return None, "FUTURE_OR_UNTIMED_FACT"
        settlement = row["settlement_evidence"]
        if (
            any(
                settlement.get(k) is not True
                for k in (
                    "exchange_flat",
                    "orders_terminal",
                    "fills_complete",
                    "cash_complete",
                )
            )
            or settlement.get("ledger_revision") != row["settlement_revision"]
            or type(settlement.get("observed_at_ms")) is not int
            or not row["latest_fill_ms"] <= settlement["observed_at_ms"] <= asof_ms
            or row["currency"] != "USDT"
            or settlement.get("account_scope") != asdict(scope)
        ):
            return None, "INCOMPLETE_SETTLEMENT"
        prior_family = _family(row["plan"], row["source"], row["regime"])
        if prior_family != family:
            return None, "FAMILY_MISMATCH"
        quantity = _decimal(row["opened_quantity"], positive=True)
        if quantity != _decimal(row["closed_quantity"], positive=True):
            return None, "INCOMPLETE_FILLS"
        net = _decimal(settlement["net_pnl"])
        if net != _decimal(row["net_pnl"]):
            return None, "SETTLEMENT_NET_MISMATCH"
        notional = _decimal(row["opening_notional"], positive=True)
        stop = _decimal(row["stop_price"], positive=True)
        with money_context():
            risk = notional - quantity * stop
            if family["side"] == "SHORT":
                risk = -risk
            if risk <= 0:
                return None, "INVALID_INITIAL_STOP_RISK"
            multiple = net / risk
        return {
            "episode_id": row["episode_id"],
            "symbol": row["symbol"],
            "closed_at_ms": row["closed_at_ms"],
            "settled_at_ms": row["settled_at_ms"],
            "latest_fill_known_at_ms": row["latest_fill_known_at_ms"],
            "decision_evidence_ref": row["evidence_ref"],
            "settlement_revision": row["settlement_revision"],
            "settlement_digest": digest(canonical(settlement)),
            "net_pnl": decimal_text(net),
            "initial_stop_risk": decimal_text(risk),
            "net_r": _text(multiple),
        }, None
    except (KeyError, ValueError, TypeError, AttributeError, InvalidOperation):
        return None, "MISSING_OR_INVALID_HISTORY_FACT"


def _stats(samples):
    if not samples:
        return {
            "count": 0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "net_pnl": "0",
            "win_rate": None,
            "mean_r": None,
            "profit_factor": None,
        }
    with money_context():
        pnls = [Decimal(s["net_pnl"]) for s in samples]
        wins, losses = sum(p > 0 for p in pnls), sum(p < 0 for p in pnls)
        positive, negative = (
            sum(p for p in pnls if p > 0),
            -sum(p for p in pnls if p < 0),
        )
        return {
            "count": len(samples),
            "wins": wins,
            "losses": losses,
            "breakeven": len(samples) - wins - losses,
            "net_pnl": _text(sum(pnls)),
            "win_rate": _text(Decimal(wins) / len(samples)),
            "mean_r": _text(sum(Decimal(s["net_r"]) for s in samples) / len(samples)),
            "profit_factor": None if negative == 0 else _text(positive / negative),
        }


def summarize(
    rows,
    *,
    scope,
    producer,
    signal,
    context,
    plan,
    controls=None,
    asof_ms=None,
    data_asof_ms=None,
    truncated=False,
    read_error=None,
):
    """Pure rollup over bounded frozen facts; no database/network side effects."""
    asof_ms = context["assembled_at"] if asof_ms is None else asof_ms
    binding = _binding(scope, producer, signal, context, plan, asof_ms)
    settings, version, control_digest = _controls(controls, scope)
    data_asof_ms = asof_ms if data_asof_ms is None else data_asof_ms
    if type(data_asof_ms) is not int or not 0 <= data_asof_ms <= asof_ms:
        raise ValueError("FUTURE_HISTORY_SNAPSHOT")
    if len(rows) > settings["history.max_samples"]:
        raise ValueError("UNBOUNDED_HISTORY_ROWS")
    start = max(0, asof_ms - settings["history.lookback_days"] * 86400000)
    excluded, samples, seen, excluded_samples = Counter(), [], set(), []
    for row in rows:
        sample, rejection = _sample(
            row,
            scope,
            producer,
            binding["family"],
            start_ms=start,
            asof_ms=data_asof_ms,
        )
        if rejection:
            excluded[rejection] += 1
            excluded_samples.append(
                {
                    "episode_id": row.get("episode_id")
                    if isinstance(row, dict)
                    else None,
                    "reason": rejection,
                }
            )
        elif sample["episode_id"] in seen:
            excluded["DUPLICATE_EPISODE"] += 1
            excluded_samples.append(
                {"episode_id": sample["episode_id"], "reason": "DUPLICATE_EPISODE"}
            )
        else:
            seen.add(sample["episode_id"])
            samples.append(sample)
    specific = [s for s in samples if s["symbol"] == signal["symbol"]]
    other = [s for s in samples if s["symbol"] != signal["symbol"]]
    family_stats, symbol_stats, prior_stats = (
        _stats(samples),
        _stats(specific),
        _stats(other),
    )
    enough_symbol = len(specific) >= settings["history.min_samples"]
    selected = specific if enough_symbol else samples
    selected_stats = symbol_stats if enough_symbol else family_stats
    enough = len(selected) >= settings["history.min_samples"]
    prior_mean = (
        Decimal(prior_stats["mean_r"])
        if enough_symbol and len(other) >= settings["history.min_samples"]
        else Decimal(0)
    )
    with money_context():
        weight = settings["history.prior_weight"]
        posterior = (
            (sum(Decimal(s["net_r"]) for s in selected) + prior_mean * weight)
            / (len(selected) + weight)
            if selected
            else None
        )
    status = "SUFFICIENT" if enough else "UNKNOWN_INSUFFICIENT_HISTORY"
    if read_error is not None:
        status = "UNKNOWN_READ_FAILED"
    if settings["history.mode"] == "OFF":
        status = "DISABLED"
    adverse = status == "SUFFICIENT" and posterior < 0
    suggested = settings["history.soft_factor"] if adverse else "1"
    applied = (
        suggested
        if settings["history.mode"] == "SOFT"
        and scope.environment == "SANDBOX"
        and asof_ms - data_asof_ms <= 30000
        else "1"
    )
    proof = {
        "schema": "layered-history-v1",
        **binding,
        "controls_version": version,
        "controls_digest": control_digest,
        "settings": settings,
        "mode": settings["history.mode"],
        "data_asof_ms": data_asof_ms,
        "lookback_start_ms": start,
        "truncated": bool(truncated),
        "queried_count": len(rows),
        "excluded": dict(sorted(excluded.items())),
        "excluded_samples": excluded_samples,
        "eligible_count": len(samples),
        "symbol_stats": symbol_stats,
        "family_stats": family_stats,
        "selected_level": "SYMBOL" if enough_symbol else "FAMILY",
        "selected_stats": selected_stats,
        "prior_mean_r": _text(prior_mean),
        "posterior_mean_r": None if posterior is None else _text(posterior),
        "status": status,
        "reason": "NEGATIVE_NET_R" if adverse else status,
        "suggested_factor": suggested,
        "applied_factor": applied,
        "samples": samples,
        "read_error": read_error,
        "risk_definition": "side_signed_actual_open_notional_minus_quantity_times_original_stop",
    }
    return _freeze(proof)


def validate(proof, signal, context, plan, controls):
    """Pure replay verification; an unverifiable soft factor cannot be applied."""
    if not isinstance(proof, dict):
        raise TypeError("LAYERED_HISTORY_EVIDENCE_REQUIRED")
    body = {k: v for k, v in proof.items() if k != "digest"}
    if proof.get("digest") != digest(canonical(body)):
        raise ValueError("LAYERED_HISTORY_DIGEST_MISMATCH")
    scope = AccountScope(**context["account_scope"])
    settings, version, control_digest = _controls(controls, scope)
    plan_dict = asdict(plan) if is_dataclass(plan) else plan
    binding = _binding(
        scope,
        plan_dict["strategy"].lower(),
        signal,
        context,
        plan,
        context["assembled_at"],
    )
    if any(proof.get(k) != v for k, v in binding.items()) or (
        proof.get("schema") != "layered-history-v1"
        or proof.get("settings") != settings
        or proof.get("controls_version") != version
        or proof.get("controls_digest") != control_digest
        or proof.get("mode") != settings["history.mode"]
    ):
        raise ValueError("LAYERED_HISTORY_SCOPE_OR_POLICY_MISMATCH")
    if (
        type(proof.get("data_asof_ms")) is not int
        or not 0 <= proof["data_asof_ms"] <= proof["asof_ms"]
    ):
        raise ValueError("FUTURE_HISTORY_SNAPSHOT")
    factor = _decimal(proof["applied_factor"], positive=True)
    if factor > 1:
        raise ValueError("LAYERED_HISTORY_CANNOT_INCREASE_RISK")
    expected = "1"
    if (
        settings["history.mode"] == "SOFT"
        and scope.environment == "SANDBOX"
        and (
            proof["status"] == "SUFFICIENT"
            and proof["selected_stats"]["count"] >= settings["history.min_samples"]
            and _decimal(proof["posterior_mean_r"]) < 0
            and 0 <= proof["asof_ms"] - proof["data_asof_ms"] <= 30000
        )
    ):
        expected = settings["history.soft_factor"]
    if factor != Decimal(expected):
        raise ValueError("LAYERED_HISTORY_FACTOR_MISMATCH")
    return proof["applied_factor"]


class LayeredHistory:
    """One bounded two-second read per account/strategy cache window (30 s).

    The cache holds only derived read-only facts, not durable business state.
    A cached observation never moves its knowledge timestamp forward.
    """

    def __init__(self, connect, *, scope, producer):
        if not isinstance(scope, AccountScope) or producer not in {"s6", "s8"}:
            raise ValueError("SCOPED_DIRECTIONAL_HISTORY_REQUIRED")
        self.connect, self.scope, self.producer = connect, scope, producer
        self._cache = None

    def evaluate(self, signal, context, plan, controls=None, *, asof_ms=None):
        now = context["assembled_at"] if asof_ms is None else asof_ms
        _binding(self.scope, self.producer, signal, context, plan, now)
        settings, _, control_digest = _controls(controls, self.scope)
        common = {
            "scope": self.scope,
            "producer": self.producer,
            "signal": signal,
            "context": context,
            "plan": plan,
            "controls": controls,
            "asof_ms": now,
        }
        if settings["history.mode"] == "OFF":
            return summarize([], **common)
        key = (
            control_digest,
            settings["history.lookback_days"],
            settings["history.max_samples"],
        )
        if self._cache is not None:
            cached_key, cached_at, rows, truncated, read_error = self._cache
            if key == cached_key and 0 <= now - cached_at <= 30000:
                return summarize(
                    rows,
                    data_asof_ms=cached_at,
                    truncated=truncated,
                    read_error=read_error,
                    **common,
                )
        try:
            with self.connect() as conn:
                conn.execute("SET LOCAL statement_timeout='2s'")
                rows = conn.execute(
                    _SQL,
                    (
                        *asdict(self.scope).values(),
                        self.producer,
                        max(0, now - settings["history.lookback_days"] * 86400000),
                        now,
                        now,
                        settings["history.max_samples"] + 1,
                    ),
                ).fetchall()
            truncated = len(rows) > settings["history.max_samples"]
            rows = [
                dict(zip(_COLUMNS, row, strict=True))
                for row in rows[: settings["history.max_samples"]]
            ]
            self._cache = key, now, rows, truncated, None
            return summarize(rows, truncated=truncated, **common)
        except Exception as exc:  # noqa: BLE001 - diagnostic research cannot halt trading
            _LOG.warning("Layered history unavailable (%s)", type(exc).__name__)
            self._cache = key, now, [], False, "HISTORY_READ_FAILED"
            return summarize([], read_error="HISTORY_READ_FAILED", **common)
