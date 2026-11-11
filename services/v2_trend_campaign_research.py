"""Append-only SANDBOX residual-trend research, never a trading dependency.

The default readers discover account scopes from ledger facts, not credentials.
Short PG read snapshots finish before bounded archive reads. Only the dedicated
research table is written. An episode failure cannot stop the next account.
Actual episode settlement and simulated residual outcomes are deliberately NOT
subtracted: their quantities, fee assumptions and holding windows differ.
"""

import re
import time
from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal, localcontext

from services.v2_stagnation_research_evidence import (
    VERSION as STAGNATION_EVIDENCE_VERSION,
)
from services.v2_stagnation_research_evidence import (
    compare_frozen_exit,
    load_frozen_exit,
)
from v2_core.evidence import canonical, digest
from v2_core.trend_runner import VERSION as RUNNER_VERSION
from v2_core.trend_runner import StagnationShadowPolicy, TrendRunnerPolicy, run_shadow

VERSION = "sandbox-trend-campaign-research-v1"
HOUR = 3_600_000
DAY = 24 * HOUR
_FINAL = {"ANALYZED", "INELIGIBLE", "NOT_APPLICABLE"}
_TRADE_FIELDS = (
    "symbol",
    "side",
    "source",
    "entry_mode",
    "entry_profile",
    "config_digest",
    "strategy_version",
    "opened_ms",
    "closed_ms",
    "opening_quantity",
    "closing_quantity",
    "opening_notional",
    "closing_notional",
    "fees_usdt",
    "net_pnl",
    "settlement_state",
    "settlement_revision",
    "settled_ms",
    "exit_reason",
    "maintenance",
)
LIMITATIONS = [
    "SANDBOX fills and SANDBOX archived prices only; LIVE positioning is not joined or priced into this study.",
    "Recent settled non-maintenance episodes are selected without filtering profit, symbol or signal score; unsettled trades are not this cohort.",
    "Historical archive receipt and native protection availability are unproven; retrospective research, not a causal live backtest.",
    "The fork is the actually held residual at the first full hour after all entry fills; earlier-flat episodes are explicitly not applicable.",
    "Both runner arms are models of that residual, not replays of the full actual strategy or its portfolio.",
    "Funding beyond actual flat is not established; missing funding never becomes zero or a net-profit claim.",
    "Fee and slippage rates are fixed research assumptions, not verified execution or available liquidity.",
    "An arm still open at the fixed observation horizon is censored, not closed at an invented terminal price.",
    "No additions, re-entry, leverage increase, automatic parameter promotion or order authority is implemented.",
]


@dataclass(frozen=True)
class CampaignPolicy:
    max_episodes: int = 5
    max_scopes: int = 10
    lookback_days: int = 7
    discovery_limit: int = 500
    history_hours: int = 60
    forward_hours: int = 6
    maturity_grace_ms: int = 300_000
    retry_ms: int = HOUR
    max_archive_minutes: int = 60_480
    max_archive_seconds: int = 60
    max_run_seconds: int = 180

    def __post_init__(self):
        for field, low, high in (
            ("max_episodes", 1, 10),
            ("max_scopes", 1, 10),
            ("lookback_days", 1, 14),
            ("discovery_limit", 1, 500),
            ("history_hours", 24, 72),
            ("forward_hours", 1, 24),
            ("maturity_grace_ms", 0, HOUR),
            ("retry_ms", 60_000, DAY),
            ("max_archive_minutes", 1440, 60_480),
            ("max_archive_seconds", 1, 90),
            ("max_run_seconds", 1, 240),
        ):
            value = getattr(self, field)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("TREND_CAMPAIGN_POLICY_BOUND")
        if self.lookback_days not in {1, 7, 14}:
            raise ValueError("TREND_CAMPAIGN_REVIEW_WINDOW")

    @property
    def fingerprint(self):
        return digest(
            canonical(
                {
                    "version": VERSION,
                    "runner_version": RUNNER_VERSION,
                    "worker": asdict(self),
                    "runner": asdict(TrendRunnerPolicy(mode="SHADOW")),
                    "stagnation_evidence_version": STAGNATION_EVIDENCE_VERSION,
                    "stagnation_shadow": asdict(StagnationShadowPolicy(mode="SHADOW")),
                }
            )
        )


def _read_only(conn):
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    conn.execute("SET LOCAL statement_timeout='5s'")
    conn.execute("SET LOCAL lock_timeout='250ms'")


def _scope_key(scope):
    from v2_core.account_risk import AccountScope

    if not isinstance(scope, AccountScope) or scope.environment != "SANDBOX":
        raise ValueError("TREND_CAMPAIGN_SANDBOX_ONLY")
    return digest(scope.key)


def _trade_facts(row):
    result = {key: row.get(key) for key in _TRADE_FIELDS}
    result["original_stop_price"] = (row.get("sizing") or {}).get("stop_price")
    return result


def _keys(scope, row):
    scope_key = _scope_key(scope)
    identity = row.get("id")
    if not isinstance(identity, str) or not 1 <= len(identity) <= 128:
        raise ValueError("TREND_CAMPAIGN_EPISODE_ID")
    return (
        scope_key,
        digest(canonical({"scope": scope_key, "episode": identity})),
        digest(canonical(_trade_facts(row))),
    )


def load_scopes(connect, *, now_ms, policy):
    """Bounded ledger-only discovery; no account registry or secret access."""
    from v2_core.account_risk import AccountScope

    with connect() as conn:
        _read_only(conn)
        rows = conn.execute(
            """SELECT exchange,account_id,environment,product FROM v2_trade_intents
            WHERE exchange='BINANCE' AND environment='SANDBOX' AND product='FUTURES'
              AND created_at>=to_timestamp(%s/1000.0)
              AND created_at<=to_timestamp(%s/1000.0)
            GROUP BY exchange,account_id,environment,product
            ORDER BY max(created_at) DESC,account_id LIMIT %s""",
            (max(0, now_ms - policy.lookback_days * DAY), now_ms, policy.max_scopes),
        ).fetchall()
    return [AccountScope(*row) for row in rows]


def load_cases(connect, scope, *, now_ms, policy, limit):
    """Select unseen/retryable recent settlements, never winners-only.

    A changed current settlement revision/fact fingerprint is new evidence.
    Immutable final results for unchanged facts are skipped. Missing archive
    retries are interleaved with new cases so neither lane monopolizes a batch.
    """
    from services.v2_profit_capture_review import strict_trade_review

    scope_key = _scope_key(scope)
    with connect() as conn:
        _read_only(conn)
        review = strict_trade_review(
            conn,
            scope,
            days=policy.lookback_days,
            now_ms=now_ms,
            limit=policy.discovery_limit,
        )
        ids = [r["id"] for r in review["trades"]]
        profiles = (
            dict(
                conn.execute(
                    """SELECT i.intent_id::text,
                e.snapshot->'features'->'evaluation'->'source_scoring'->>'profile'
            FROM v2_trade_intents i JOIN v2_decision_evidence e USING(evidence_ref)
            WHERE i.intent_id=ANY(%s::uuid[])
              AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
              AND e.created_at<=to_timestamp(%s/1000.0)""",
                    (ids, *asdict(scope).values(), now_ms),
                ).fetchall()
            )
            if ids
            else {}
        )
        previous = conn.execute(
            """SELECT DISTINCT ON(episode_digest,facts_digest)
                   episode_digest,facts_digest,status,ready_at_ms,
                   (extract(epoch FROM created_at)*1000)::bigint
            FROM v2_trend_campaign_reports
            WHERE method_version=%s AND policy_digest=%s AND scope_digest=%s
              AND created_at>=to_timestamp(%s/1000.0)
            ORDER BY episode_digest,facts_digest,created_at DESC,report_digest DESC
            LIMIT 2000""",
            (
                VERSION,
                policy.fingerprint,
                scope_key,
                max(0, now_ms - (policy.lookback_days + 2) * DAY),
            ),
        ).fetchall()
        known = {(r[0], r[1]): r[2:] for r in previous}
        lanes = ([], [])
        for row in sorted(
            review["trades"],
            key=lambda r: (r.get("opened_ms") or 0, r["id"]),
            reverse=True,
        ):
            row = {**row, "entry_profile": profiles.get(row["id"])}
            if (
                row.get("settlement_state") != "SETTLED"
                or row.get("closed_ms") is None
                or row.get("net_pnl") is None
                or row.get("maintenance")
            ):
                continue
            _, episode, facts = _keys(scope, row)
            old = known.get((episode, facts))
            if old and (
                old[0] in _FINAL
                or old[0] == "PENDING"
                and now_ms < old[1]
                or old[0] == "UNKNOWN"
                and now_ms < old[2] + policy.retry_ms
            ):
                continue
            lanes[1 if old else 0].append(row)
        selected = []
        for index in range(max(map(len, lanes))):
            for lane in lanes:
                if index < len(lane) and len(selected) < limit:
                    selected.append(lane[index])
        cases = []
        for row in selected:
            fills = conn.execute(
                """SELECT f.fill_key,f.occurred_at_ms,o.leg,f.quantity::text,f.price::text
                FROM v2_trade_intents i JOIN v2_orders o ON o.episode_id=i.intent_id
                JOIN v2_fills f USING(order_id)
                WHERE i.intent_id=%s
                  AND (i.exchange,i.account_id,i.environment,i.product)=(%s,%s,%s,%s)
                  AND f.occurred_at_ms<=%s AND f.created_at<=to_timestamp(%s/1000.0)
                ORDER BY f.occurred_at_ms,f.fill_key LIMIT 1001""",
                (row["id"], *asdict(scope).values(), now_ms, now_ms),
            ).fetchall()
            cases.append(
                {
                    "trade": row,
                    "fills": [
                        dict(
                            zip(
                                ("identity", "t", "leg", "quantity", "price"),
                                f,
                                strict=True,
                            )
                        )
                        for f in fills
                    ],
                    "source_records_truncated": review["truncated"],
                    "stagnation_evidence": load_frozen_exit(
                        conn, scope, row, as_of_ms=now_ms
                    ),
                }
            )
    return cases


def _reason(exc):
    value = str(exc)
    if re.fullmatch(
        r"(?:PROFIT_AUDIT|TIMEFRAME|TREND_CAMPAIGN)_[A-Z0-9_]{1,80}", value
    ):
        return value
    return "TREND_CAMPAIGN_SOURCE_OR_COMPUTE_ERROR"


def _seal(report, inputs):
    report["input_digest"] = digest(canonical(inputs))
    report["report_digest"] = digest(canonical(report))
    return report


def evaluate(case, scope, read, *, now_ms, policy):
    """One actual settled episode; no writes and no implicit live-data fallback."""
    from services.v2_profitability_audit import AuditPolicy, runner_input, validate_case

    row = case["trade"]
    scope_key, episode, facts = _keys(scope, row)
    report = {
        "method_version": VERSION,
        "policy_digest": policy.fingerprint,
        "scope_digest": scope_key,
        "episode_digest": episode,
        "facts_digest": facts,
        "market_source": "BINANCE_USDM_SANDBOX",
        "environment": "SANDBOX",
        "status": "UNKNOWN",
        "reason": None,
        "ready_at_ms": None,
        "symbol": row.get("symbol"),
        "side": row.get("side"),
        "entry_profile": row.get("entry_profile") or "UNRECORDED_LEGACY_OR_UNKNOWN",
        "strategy_version": row.get("strategy_version"),
        "config_digest": row.get("config_digest"),
        "actual_baseline": None,
        "residual_shadow": None,
        "stagnation_exit_shadow": compare_frozen_exit(case.get("stagnation_evidence")),
        "input_digest_complete": False,
        "source_records_truncated": bool(case.get("source_records_truncated", False)),
        "historical_receipt_proven": False,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
        "net_pnl_delta_vs_actual": None,
        "limitations": LIMITATIONS,
    }
    inputs = {
        "facts_digest": facts,
        "policy_digest": policy.fingerprint,
        "market_source": report["market_source"],
        "source_records_truncated": report["source_records_truncated"],
        "stagnation_evidence_digest": digest(
            canonical(report["stagnation_exit_shadow"])
        ),
    }
    try:
        if (
            row.get("settlement_state") != "SETTLED"
            or row.get("net_pnl") is None
            or row.get("maintenance")
            or row.get("closed_ms") is None
        ):
            raise ValueError("TREND_CAMPAIGN_SETTLED_NON_MAINTENANCE_REQUIRED")
        validate_case(case, as_of_ms=now_ms)
        inputs["actual_fills_digest"] = digest(canonical({"fills": case["fills"]}))
        opens = [f for f in case["fills"] if f["leg"] == "OPEN"]
        activation = (max(f["t"] for f in opens) // HOUR + 1) * HOUR
        end = activation + policy.forward_hours * HOUR
        report["ready_at_ms"] = end + policy.maturity_grace_ms
        report["activation_at_ms"] = activation
        report["observation_end_ms"] = end
        with localcontext() as ctx:
            ctx.prec = 80
            sign = Decimal(1 if row["side"] == "BUY" else -1)
            gross = sign * (
                Decimal(row["closing_notional"]) - Decimal(row["opening_notional"])
            )
        report["actual_baseline"] = {
            "basis": "WHOLE_ACTUAL_EPISODE_CURRENT_COMPLETE_USDT_SETTLEMENT",
            "opened_ms": row["opened_ms"],
            "closed_ms": row["closed_ms"],
            "net_pnl_usdt": row["net_pnl"],
            "gross_pnl_usdt": str(gross),
            "fees_usdt": row.get("fees_usdt"),
            "settlement_revision": row.get("settlement_revision"),
            "funding_cashflow_usdt": None,
            "funding_status": "NOT_SEPARATELY_LOADED",
            "exit_reason": row.get("exit_reason"),
        }
        if row["closed_ms"] <= activation:
            report.update(
                status="NOT_APPLICABLE", reason="NO_ACTUAL_RESIDUAL_AT_ACTIVATION"
            )
        elif now_ms < report["ready_at_ms"]:
            report.update(status="PENDING", reason="FIXED_FORWARD_WINDOW_NOT_MATURE")
        else:

            def complete_sandbox_path(symbol, start, stop):
                bars = read(symbol, start, stop)
                if not isinstance(bars, list) or sorted(
                    b.get("t") for b in bars
                ) != list(range(start, stop, 60_000)):
                    raise ValueError("TREND_CAMPAIGN_INCOMPLETE_MINUTE_ARCHIVE")
                if any(
                    b.get("environment", "SANDBOX") != "SANDBOX"
                    or b.get("market_source", "BINANCE_USDM_SANDBOX")
                    != "BINANCE_USDM_SANDBOX"
                    or b.get("symbol", symbol) != symbol
                    for b in bars
                ):
                    raise ValueError("TREND_CAMPAIGN_ARCHIVE_SOURCE_MISMATCH")
                return sorted(bars, key=lambda b: b["t"])

            payload = runner_input(
                case,
                complete_sandbox_path,
                as_of_ms=now_ms,
                policy=AuditPolicy(
                    max_trades=1,
                    trend_cases=1,
                    trend_history_hours=policy.history_hours,
                    trend_forward_hours=policy.forward_hours,
                ),
            )
            if payload["as_of_ms"] != end or payload["activation_at_ms"] != activation:
                raise ValueError("TREND_CAMPAIGN_FIXED_WINDOW_MISMATCH")
            inputs["residual_input"] = payload
            shadow = run_shadow(payload, policy=TrendRunnerPolicy(mode="SHADOW"))
            # The engine correctly marks net as unknown, but its empty funding
            # accumulator is numerically zero. Here unknown coverage is always
            # rendered as UNKNOWN/None, never exposed as observed zero funding.
            shadow["costs"]["funding_status"] = (
                "UNKNOWN_SANDBOX_FUNDING_PATH_NOT_LOADED"
            )
            for arm in shadow.get("arms", {}).values():
                arm["reported_funding_cashflow"] = None
                arm["funding_completeness"] = "UNKNOWN"
                arm["modeled_net_with_reported_funding"] = None
            report["residual_shadow"] = shadow
            report["input_digest_complete"] = True
            report["status"] = {"SHADOW": "ANALYZED", "INELIGIBLE": "INELIGIBLE"}.get(
                shadow["status"], "UNKNOWN"
            )
            report["reason"] = shadow.get("reason")
    except Exception as exc:  # noqa: BLE001 - sanitize and isolate one episode
        report.update(status="UNKNOWN", reason=_reason(exc))
    inputs["evaluation_status"] = report["status"]
    inputs["evaluation_reason"] = report["reason"]
    return _seal(report, inputs)


def persist(connect, report, *, now_ms):
    """Only INSERT into the research table; duplicate input never overwrites."""
    if (
        report.get("execution_authorized") is not False
        or report.get("automatic_parameter_updates") is not False
        or report.get("promotion_authorized") is not False
        or report.get("environment") != "SANDBOX"
        or report.get("method_version") != VERSION
        or report.get("status") not in _FINAL | {"PENDING", "UNKNOWN"}
    ):
        raise ValueError("TREND_CAMPAIGN_PERSIST_SCOPE")
    if type(now_ms) is not int or not 0 <= now_ms < 2**53:
        raise ValueError("TREND_CAMPAIGN_PERSIST_CLOCK")
    for key in (
        "scope_digest",
        "episode_digest",
        "facts_digest",
        "policy_digest",
        "input_digest",
        "report_digest",
    ):
        if not re.fullmatch(r"[0-9a-f]{64}", str(report.get(key, ""))):
            raise ValueError("TREND_CAMPAIGN_DIGEST_INVALID")
    if (
        digest(canonical({k: v for k, v in report.items() if k != "report_digest"}))
        != report["report_digest"]
    ):
        raise ValueError("TREND_CAMPAIGN_REPORT_DIGEST_MISMATCH")
    content = canonical(report)
    if len(content.encode()) > 262_144:
        raise ValueError("TREND_CAMPAIGN_REPORT_BOUND")
    with connect() as conn:
        conn.execute("SET LOCAL statement_timeout='5s'")
        conn.execute("SET LOCAL lock_timeout='250ms'")
        row = conn.execute(
            """INSERT INTO v2_trend_campaign_reports
            (method_version,policy_digest,scope_digest,episode_digest,facts_digest,
             input_digest,report_digest,status,ready_at_ms,report,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,to_timestamp(%s/1000.0))
            ON CONFLICT(method_version,policy_digest,episode_digest,input_digest)
            DO NOTHING RETURNING report_digest""",
            (
                VERSION,
                report["policy_digest"],
                report["scope_digest"],
                report["episode_digest"],
                report["facts_digest"],
                report["input_digest"],
                report["report_digest"],
                report["status"],
                report["ready_at_ms"],
                content,
                now_ms,
            ),
        ).fetchone()
    return "INSERTED" if row else "EXISTING"


def run(
    connect,
    archive=None,
    *,
    now_ms,
    policy=None,
    scope_reader=None,
    case_reader=None,
    report_writer=None,
    monotonic=time.monotonic,
):
    """Dependency-injected bounded driver for an independent timer process.

    scope_reader(connect, *, now_ms, policy) -> list[AccountScope]
    case_reader(connect, scope, *, now_ms, policy, limit) -> list[case]
    report_writer(connect, report, *, now_ms) -> persistence status
    archive: explicitly SANDBOX object with read(symbol,start,end). A production
    adapter must label its environment; ReentryArchive is accepted by type and
    created lazily when omitted. No transaction spans a market archive read.
    """
    from services.v2_profitability_audit import AuditPolicy, BudgetArchive
    from services.v2_reentry_archive import ReentryArchive

    policy = CampaignPolicy() if policy is None else policy
    if (
        not isinstance(policy, CampaignPolicy)
        or type(now_ms) is not int
        or not 0 <= now_ms < 2**53
    ):
        raise ValueError("TREND_CAMPAIGN_RUN_BOUNDS")
    own_archive = archive is None
    archive = ReentryArchive() if own_archive else archive
    if (
        not isinstance(archive, ReentryArchive)
        and getattr(archive, "environment", None) != "SANDBOX"
    ):
        raise ValueError("TREND_CAMPAIGN_ARCHIVE_SANDBOX_ONLY")
    scope_reader = scope_reader or load_scopes
    case_reader = case_reader or load_cases
    report_writer = report_writer or persist
    started = monotonic()
    read = BudgetArchive(
        archive.read,
        AuditPolicy(
            max_archive_minutes=policy.max_archive_minutes,
            max_archive_seconds=policy.max_archive_seconds,
        ),
        monotonic=monotonic,
    )
    result = {
        "method_version": VERSION,
        "status": "COMPLETE",
        "outcomes": [],
        "errors": [],
        "execution_authorized": False,
        "promotion_authorized": False,
        "automatic_parameter_updates": False,
        "policy_digest": policy.fingerprint,
    }
    try:
        try:
            scopes = scope_reader(connect, now_ms=now_ms, policy=policy)
            if not isinstance(scopes, list) or len(scopes) > policy.max_scopes:
                raise ValueError("TREND_CAMPAIGN_SCOPE_BOUND")
            keys = [_scope_key(scope) for scope in scopes]
            if len(set(keys)) != len(keys):
                raise ValueError("TREND_CAMPAIGN_DUPLICATE_SCOPE")
            result["scope_discovery_limit"] = policy.max_scopes
            result["scope_discovery_at_limit"] = len(scopes) == policy.max_scopes
        except Exception as exc:  # noqa: BLE001 - job failure never calls trading
            result.update(
                status="UNAVAILABLE",
                errors=[{"stage": "SCOPES", "reason": _reason(exc)}],
            )
            return result
        # Rotate first account deterministically so a busy account cannot always
        # consume the global cap before another valid account gets inspected.
        offset = (now_ms // policy.retry_ms) % len(scopes) if scopes else 0
        scopes = scopes[offset:] + scopes[:offset]
        for scope in scopes:
            remaining = policy.max_episodes - len(result["outcomes"])
            if remaining <= 0 or monotonic() - started >= policy.max_run_seconds:
                result["status"] = "BOUNDED"
                break
            try:
                cases = case_reader(
                    connect, scope, now_ms=now_ms, policy=policy, limit=remaining
                )
                if not isinstance(cases, list) or len(cases) > remaining:
                    raise ValueError("TREND_CAMPAIGN_EPISODE_BOUND")
            except Exception as exc:  # noqa: BLE001 - another account still runs
                result["errors"].append(
                    {
                        "stage": "CASES",
                        "scope_digest": _scope_key(scope),
                        "reason": _reason(exc),
                    }
                )
                continue
            for case in cases:
                if monotonic() - started >= policy.max_run_seconds:
                    result["status"] = "BOUNDED"
                    break
                try:
                    report = evaluate(case, scope, read, now_ms=now_ms, policy=policy)
                    persisted = report_writer(connect, report, now_ms=now_ms)
                    result["outcomes"].append(
                        {
                            "episode_digest": report["episode_digest"],
                            "status": report["status"],
                            "reason": report["reason"],
                            "report_digest": report["report_digest"],
                            "persistence": persisted,
                        }
                    )
                except Exception as exc:  # noqa: BLE001 - isolated invalid input/write outage
                    result["outcomes"].append(
                        {
                            "status": "UNKNOWN",
                            "reason": _reason(exc),
                            "persistence": "FAILED",
                        }
                    )
        result["status_counts"] = dict(Counter(r["status"] for r in result["outcomes"]))
        if result["errors"] or any(
            r.get("persistence") == "FAILED" for r in result["outcomes"]
        ):
            result["status"] = "PARTIAL"
        return result
    finally:
        result["archive_calls"] = read.calls
        result["requested_archive_minutes"] = read.requested_minutes
        if own_archive:
            archive.close()
