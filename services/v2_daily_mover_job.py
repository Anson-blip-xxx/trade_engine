"""Daily mover review orchestrator: research writes only; never trade execution."""

import json
import time
from datetime import date, timedelta

from services.v2_daily_mover_evidence import load_daily_mover_evidence
from services.v2_daily_mover_flow import unavailable_review
from services.v2_daily_mover_market import MoverMarketError
from services.v2_daily_mover_precursors import VERSION as PRECURSOR_VERSION
from services.v2_daily_mover_precursors import build_precursor_review
from services.v2_daily_mover_report import build_report, format_digest, seal_report
from services.v2_daily_mover_trade_paths import enrich_case_paths, load_case_fills
from services.v2_mover_flow_evaluation import VERSION as FLOW_EVALUATION_VERSION
from services.v2_mover_flow_evaluation import unavailable_evaluation
from v2_core.daily_movers import (
    DAY,
    VERSION,
    analyze_intraday_path,
    previous_day_window,
    rank_daily_movers,
)
from v2_core.evidence import canonical, digest

SOURCE = "BINANCE_USDM_LIVE"


def emit(**fields):
    print(json.dumps(fields, ensure_ascii=False, separators=(",", ":")), flush=True)


class DailyMoverJob:
    def __init__(
        self,
        store,
        market,
        *,
        clock_ms=lambda: time.time_ns() // 1000000,
        evidence_loader=load_daily_mover_evidence,
        notifier=None,
        path_reader=None,
        case_loader=load_case_fills,
        force_evidence_refresh=False,
        flow_reviewer=None,
        flow_evaluator=None,
    ):
        self.store, self.market, self.clock = store, market, clock_ms
        self.evidence_loader, self.notifier = evidence_loader, notifier
        self.path_reader, self.case_loader = path_reader, case_loader
        if type(force_evidence_refresh) is not bool:
            raise ValueError("MOVER_REFRESH_FLAG_INVALID")
        self.force_evidence_refresh = force_evidence_refresh
        self.flow_reviewer = flow_reviewer
        self.flow_evaluator = flow_evaluator

    def collect(self, window):
        day = window["date"]
        saved = self.store.day(day)
        if saved is None:
            catalog = self.market.catalog()
            saved = self.store.start_day(day, catalog, self.clock())
        if saved["status"] == "COMPLETE":
            return saved["ranking"], self.store.symbols(day)
        samples = self.store.symbols(day)
        expected, failures = 0, 0
        for row in saved["catalog"]:
            target = row["symbol"]
            # New listings/delisted days are separate exclusions, not zero return.
            if (
                type(row.get("onboardDate")) is int
                and row["onboardDate"] > window["start_ms"]
                or type(row.get("deliveryDate")) is int
                and row["deliveryDate"] < window["end_ms"]
            ):
                continue
            expected += 1
            prior = samples.get(target)
            if prior and prior["status"] == "COMPLETE":
                continue
            if prior and prior["attempts"] >= 3:
                failures += 1
                continue
            try:
                summary, hourly = self.market.summary(
                    target,
                    start_ms=window["start_ms"],
                    end_ms=window["end_ms"],
                    as_of_ms=self.clock(),
                )
                self.store.save_symbol(
                    day,
                    target,
                    summary,
                    hourly,
                    error=None
                    if summary.get("complete")
                    else summary.get("reason", "UNKNOWN"),
                )
                failures += summary.get("complete") is not True
            except MoverMarketError as exc:
                # Quota/network-wide failure pauses this independent job, never
                # an execution task. Already collected symbols remain durable.
                if str(exc) in {
                    "MOVER_RATE_LIMITED",
                    "MOVER_SHARED_QUOTA_WAIT",
                    "MOVER_JOB_DEADLINE",
                    "MOVER_PUBLIC_TRANSPORT_FAILURE",
                }:
                    raise
                self.store.save_symbol(day, target, None, None, error=str(exc))
                failures += 1
            if expected % 50 == 0:
                emit(
                    component="daily-mover-review",
                    stage="PUBLIC_HOURLY",
                    date=day,
                    symbols_considered=expected,
                    requests=self.market.requests,
                )
        samples = self.store.symbols(day)
        ranking = rank_daily_movers(
            saved["catalog"],
            [x["summary"] for x in samples.values() if x["summary"]],
            start_ms=window["start_ms"],
            end_ms=window["end_ms"],
            as_of_ms=self.clock(),
            universe_observed_at_ms=saved["observed_at_ms"],
        )
        ranking["market_source"] = SOURCE
        ranking["source_digest"] = digest(canonical(ranking))
        ranking["collection"] = {
            "eligible_contracts_attempted": expected,
            "unresolved": failures,
            "hourly_requests_resume_from_pg": True,
        }
        selected = ranking["winners"] + ranking["losers"]
        path_pending = False
        for row in selected:
            target = row["symbol"]
            prior_path = samples[target]["minute_path"]
            if prior_path is not None and (
                prior_path.get("analysis", {}).get("status") != "UNKNOWN"
                or samples[target].get("path_attempts", 0) >= 3
            ):
                continue
            try:
                bars = self.market.bars(
                    target, window["start_ms"], window["end_ms"], interval_minutes=1
                )
                path = analyze_intraday_path(
                    bars,
                    start_ms=window["start_ms"],
                    end_ms=window["end_ms"],
                    as_of_ms=self.clock(),
                    side="BUY" if row in ranking["winners"] else "SELL",
                    market_source=SOURCE,
                )
                # Store exact public source candles only in PG, with provenance.
                self.store.save_path(
                    day,
                    target,
                    {
                        "source": SOURCE,
                        "fetched_at_ms": self.clock(),
                        "bars_digest": digest(canonical({"bars": bars})),
                        "bars": bars,
                        "analysis": path,
                    },
                )
                path_pending |= (
                    path.get("status") == "UNKNOWN"
                    and samples[target].get("path_attempts", 0) < 2
                )
            except MoverMarketError as exc:
                if str(exc) in {
                    "MOVER_RATE_LIMITED",
                    "MOVER_SHARED_QUOTA_WAIT",
                    "MOVER_JOB_DEADLINE",
                    "MOVER_PUBLIC_TRANSPORT_FAILURE",
                }:
                    raise
                self.store.save_path(
                    day,
                    target,
                    {
                        "source": SOURCE,
                        "fetched_at_ms": self.clock(),
                        "bars": [],
                        "analysis": {"status": "UNKNOWN", "reason": str(exc)},
                    },
                )
                path_pending |= samples[target].get("path_attempts", 0) < 2
        final_samples = self.store.symbols(day)
        ranking["minute_path_digest"] = digest(
            canonical(
                {
                    r["symbol"]: final_samples[r["symbol"]]["minute_path"]
                    for r in selected
                }
            )
        )
        ranking["source_digest"] = digest(
            canonical({k: v for k, v in ranking.items() if k != "source_digest"})
        )
        self.store.finish_market(
            day, ranking, complete=failures == 0 and not path_pending
        )
        return ranking, self.store.symbols(day)

    def deliver(self, identity, report):
        if self.notifier is None:
            return "NOT_CONFIGURED_PENDING"
        if not self.store.claim_delivery(identity):
            return "ALREADY_HANDLED_OR_DELIVERY_UNKNOWN"
        try:
            message = self.notifier(
                format_digest(report, account_alias=report.get("account_alias"))
            )
            if type(message) is not int or message <= 0:
                raise ValueError("MOVER_DM_RECEIPT_INVALID")
        except Exception:  # noqa: BLE001 - delivery can be ambiguous; do not expose secrets
            self.store.delivered(identity)
            return "UNKNOWN_NO_BLIND_RESEND"
        self.store.delivered(identity, message)
        return "SENT"

    def run_day(self, window):
        ranking, samples = self.collect(window)
        day = window["date"]
        # Independent research reuses the existing hourly archive; no market
        # requests, execution hooks, or additional trading-read transactions.
        # One optional analysis failure must not block account evidence or DM.
        try:
            precursor = build_precursor_review(
                window=window,
                catalog=self.store.day(day)["catalog"],
                samples=samples,
                ranking=ranking,
                as_of_ms=self.clock(),
            )
        except Exception:  # noqa: BLE001 - bounded optional research, no raw error details
            precursor = {
                "version": PRECURSOR_VERSION,
                "status": "UNKNOWN",
                "reason": "PRECURSOR_REVIEW_UNAVAILABLE",
                "execution_authorized": False,
                "automatic_parameter_updates": False,
            }
        flow_review = unavailable_review("FLOW_RESEARCH_DISABLED")
        if self.flow_reviewer is not None:
            try:
                flow_review = self.flow_reviewer.build(
                    window=window, precursor=precursor
                )
            except Exception:  # noqa: BLE001 - optional research cannot block the old report
                flow_review = unavailable_review("FLOW_RESEARCH_UNAVAILABLE")
        flow_evaluation = unavailable_evaluation("FLOW_EVALUATION_DISABLED")
        if self.flow_evaluator is not None:
            try:
                flow_evaluation = self.flow_evaluator.build(
                    window=window,
                    precursor=precursor,
                    flow_review=flow_review,
                    as_of_ms=self.clock(),
                )
                if (
                    not isinstance(flow_evaluation, dict)
                    or flow_evaluation.get("version") != FLOW_EVALUATION_VERSION
                    or flow_evaluation.get("status")
                    not in {
                        "UNKNOWN",
                        "INSUFFICIENT_SAMPLE",
                        "DESCRIPTIVE_REVIEW_READY",
                    }
                    or any(
                        flow_evaluation.get(k) is not False
                        for k in (
                            "execution_authorized",
                            "automatic_parameter_updates",
                            "promotion_authorized",
                        )
                    )
                    or flow_evaluation.get("content_digest")
                    != digest(
                        canonical(
                            {
                                k: v
                                for k, v in flow_evaluation.items()
                                if k != "content_digest"
                            }
                        )
                    )
                    or len(canonical(flow_evaluation).encode()) > 512 * 1024
                ):
                    raise ValueError("FLOW_EVALUATION_RESULT_INVALID")
            except Exception:  # noqa: BLE001 - history/model failures never block the primary report
                flow_evaluation = unavailable_evaluation("FLOW_EVALUATION_UNAVAILABLE")
        paths = {
            s: row["minute_path"]["analysis"]
            for s, row in samples.items()
            if row["minute_path"]
        }
        targets = [r["symbol"] for r in ranking["winners"] + ranking["losers"]]
        results = []
        for scope in self.store.scopes(window["start_ms"], window["end_ms"]):
            existing = self.store.existing(scope, day)
            retry_at = (
                existing[1].get("evidence_snapshot", {}).get("retry_after_ms")
                if existing
                else None
            )
            if (
                existing
                and not self.force_evidence_refresh
                and existing[1].get("market_digest") == ranking["source_digest"]
                and existing[1].get("precursor_review", {}).get("version")
                == PRECURSOR_VERSION
                and existing[1].get("flow_review", {}).get("content_digest")
                == flow_review["content_digest"]
                and existing[1].get("flow_evaluation", {}).get("content_digest")
                == flow_evaluation["content_digest"]
                and (retry_at is None or self.clock() < retry_at)
            ):
                identity, report = existing
                results.append(
                    {"status": "EXISTING", "delivery": self.deliver(identity, report)}
                )
                continue
            # One account's missing/late evidence never blocks another account.
            evidence, loaded, read_error = None, None, False
            as_of = self.clock()
            if targets:
                try:
                    with self.store.connect() as c:
                        c.execute(
                            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                        )
                        evidence = self.evidence_loader(
                            c,
                            scope,
                            start_ms=window["start_ms"],
                            end_ms=window["end_ms"],
                            symbols=targets,
                            as_of_ms=as_of,
                            max_signals_per_symbol=250,
                            max_trades=200,
                            max_read_seconds=45,
                        )
                        if self.path_reader is not None:
                            loaded = self.case_loader(
                                c, scope, evidence, as_of_ms=as_of
                            )
                except Exception:  # noqa: BLE001 - isolate account reads, no raw DB details
                    loaded, read_error = None, True
            actual_paths = (
                {"status": "UNKNOWN", "reason": "SCOPED_FILL_READ_UNAVAILABLE"}
                if self.path_reader is not None and loaded is None and targets
                else None
            )
            if loaded is not None:
                try:
                    actual_paths = enrich_case_paths(
                        loaded, self.path_reader, as_of_ms=as_of
                    )
                except Exception:  # noqa: BLE001 - optional archive failure must be isolated
                    actual_paths = {
                        "status": "UNKNOWN",
                        "reason": "SAME_ENV_PATH_READ_FAILED",
                    }
            if isinstance(evidence, dict):
                transient = {
                    "READ_BUDGET_EXHAUSTED",
                    "QUERY_TIMEOUT",
                    "READ_PERMISSION_MISSING",
                    "READ_SCHEMA_UNAVAILABLE",
                    "READ_FAILED",
                }
                for record in evidence.get("symbols", {}).values():
                    read_error |= any(
                        t.get("label_read_error") in transient
                        for t in record.get("trades", [])
                    )
                    read_error |= any(
                        value in transient
                        for value in record.get("coverage", {}).values()
                        if isinstance(value, str)
                    )
            report = build_report(
                day,
                ranking,
                evidence,
                paths,
                as_of_ms=as_of,
                scope=scope,
                prior_reports=self.store.prior(scope, day),
            )
            report["market_digest"] = ranking["source_digest"]
            report["research_only"] = True
            report["precursor_review"] = precursor
            report["flow_review"] = flow_review
            report["flow_evaluation"] = flow_evaluation
            # Preserve complete underlying evidence alongside conclusions; no
            # snapshots/prompts/credentials are included by the evidence adapter.
            report["account_evidence"] = evidence
            report["account_alias"] = (
                self.store.alias(scope) if hasattr(self.store, "alias") else None
            )
            maturity_at = window["end_ms"] + 6 * 3600000
            path_attempts = (
                existing[1].get("evidence_snapshot", {}).get("path_attempts", 0)
                if existing
                else 0
            ) + 1
            path_retry = (
                isinstance(actual_paths, dict)
                and path_attempts < 3
                and (
                    actual_paths.get("status") == "UNKNOWN"
                    or any(
                        case.get("status") == "UNKNOWN"
                        for case in actual_paths.get("cases", [])
                    )
                )
            )
            report["evidence_snapshot"] = {
                "status": "READ_INCOMPLETE" if read_error else "READ_COMPLETED",
                "as_of_ms": as_of,
                "maturity": "INITIAL" if as_of < maturity_at else "MATURE",
                "path_attempts": path_attempts,
                "retry_after_ms": as_of + 3600000
                if read_error or path_retry
                else maturity_at
                if as_of < maturity_at
                else None,
            }
            report["actual_same_environment_paths"] = actual_paths
            report = seal_report(report)
            identity = self.store.save_report(scope, day, report)
            results.append(
                {"status": "CREATED", "delivery": self.deliver(identity, report)}
            )
        removed = self.store.prune_raw(day)
        return {
            "date": day,
            "coverage": ranking["coverage"],
            "accounts": results,
            "winners": len(ranking["winners"]),
            "losers": len(ranking["losers"]),
            "expired_raw_paths_cleared": removed,
            "orders_submitted": False,
        }

    def run(self, *, day=None):
        last = previous_day_window(self.clock())
        if day is not None:
            requested = date.fromisoformat(day)
            delta = (date.fromisoformat(last["date"]) - requested).days
            if not 0 <= delta <= 3:
                raise ValueError("MOVER_BACKFILL_MAX3_COMPLETED_DAYS")
            window = {
                **last,
                "date": day,
                "start_ms": last["start_ms"] - delta * DAY,
                "end_ms": last["end_ms"] - delta * DAY,
            }
            return [self.run_day(window)]
        with self.store.connect() as c:
            previous = c.execute(
                "SELECT max(day) FROM v2_daily_mover_days WHERE method_version=%s",
                (VERSION,),
            ).fetchone()[0]
        target = date.fromisoformat(last["date"])
        first = (
            target
            if previous is None
            else max(min(previous, target), target - timedelta(days=2))
        )
        result = []
        for offset in range((target - first).days + 1):
            current = first + timedelta(days=offset)
            delta = (target - current).days
            result.append(
                self.run_day(
                    {
                        **last,
                        "date": current.isoformat(),
                        "start_ms": last["start_ms"] - delta * DAY,
                        "end_ms": last["end_ms"] - delta * DAY,
                    }
                )
            )
        return result
