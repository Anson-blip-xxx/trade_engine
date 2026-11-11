"""Daily mover review orchestrator: research writes only; never trade execution."""

import json
import time
from datetime import date, timedelta

from services.v2_daily_account_review import JEV_VERSION, load_account_day_review
from services.v2_daily_account_review import VERSION as ACCOUNT_DAY_VERSION
from services.v2_daily_mover_coverage import VERSION as COVERAGE_VERSION
from services.v2_daily_mover_coverage import (
    build_coverage_review,
    load_execution_catalog_snapshot,
    unavailable_snapshot,
)
from services.v2_daily_mover_evidence import (
    CLASSIFICATION_VERSION,
    load_daily_mover_evidence,
)
from services.v2_daily_mover_execution_review import VERSION as EXECUTION_REVIEW_VERSION
from services.v2_daily_mover_execution_review import build_execution_review
from services.v2_daily_mover_flow import unavailable_review
from services.v2_daily_mover_market import MoverMarketError
from services.v2_daily_mover_precursors import VERSION as PRECURSOR_VERSION
from services.v2_daily_mover_precursors import build_precursor_review
from services.v2_daily_mover_replay import VERSION as FROZEN_REPLAY_VERSION
from services.v2_daily_mover_replay import load_frozen_replay_review
from services.v2_daily_mover_report import VERSION as REPORT_VERSION
from services.v2_daily_mover_report import build_report, format_digest, seal_report
from services.v2_daily_mover_settlement_refresh import (
    VERSION as SETTLEMENT_REFRESH_VERSION,
)
from services.v2_daily_mover_settlement_refresh import refresh_snapshot
from services.v2_daily_mover_trade_paths import enrich_case_paths, load_case_fills
from services.v2_mover_flow_evaluation import VERSION as FLOW_EVALUATION_VERSION
from services.v2_mover_flow_evaluation import unavailable_evaluation
from services.v2_mover_gate_shadow import POLICY_COMPATIBILITY_VERSION
from services.v2_mover_gate_shadow import VERSION as GATE_SHADOW_VERSION
from services.v2_setup_shadow_paths import VERSION as SETUP_PATH_VERSION
from services.v2_setup_shadow_paths import enrich_setup_paths
from services.v2_setup_shadow_study import VERSION as SETUP_STUDY_VERSION
from services.v2_setup_shadow_study import load_setup_study
from v2_core.daily_movers import (
    DAY,
    VERSION,
    analyze_intraday_path,
    previous_day_window,
    rank_daily_movers,
)
from v2_core.evidence import canonical, digest
from v2_core.execution_cost_evidence import VERSION as COST_EVIDENCE_VERSION
from v2_core.research_market_sources import VERSION as MARKET_SOURCES_VERSION

SOURCE = "BINANCE_USDM_LIVE"
REVIEW_COMPONENTS = {
    "pipeline": "daily-mover-review-pipeline-v5",
    "report": REPORT_VERSION,
    "classification": CLASSIFICATION_VERSION,
    "gate_shadow": GATE_SHADOW_VERSION,
    "coverage": COVERAGE_VERSION,
    "execution": EXECUTION_REVIEW_VERSION,
    "account_day": ACCOUNT_DAY_VERSION,
    "frozen_replay": FROZEN_REPLAY_VERSION,
    "setup_study": SETUP_STUDY_VERSION,
    "setup_paths": SETUP_PATH_VERSION,
    "settlement_refresh": SETTLEMENT_REFRESH_VERSION,
    "jev_participation": JEV_VERSION,
    # Revisit the scheduled day once after a reader upgrade. Existing frozen
    # study selections and day-scoped notification deduplication remain intact.
    "frozen_policy_compatibility": POLICY_COMPATIBILITY_VERSION,
    "execution_cost_evidence": COST_EVIDENCE_VERSION,
    "market_sources": MARKET_SOURCES_VERSION,
}


def _unavailable_review(version, reason):
    result = {
        "version": version,
        "status": "UNKNOWN",
        "reason": reason,
        "coverage": {"query_complete": False, "retryable": True},
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
    }
    return {**result, "content_digest": digest(canonical(result))}


def _validated_review(result, version):
    if (
        not isinstance(result, dict)
        or result.get("version") != version
        or not isinstance(result.get("status"), str)
        or any(
            result.get(flag) is not False
            for flag in (
                "execution_authorized",
                "automatic_parameter_updates",
                "promotion_authorized",
            )
        )
    ):
        raise ValueError("OPTIONAL_REVIEW_INVALID")
    encoded = json.dumps(
        {k: v for k, v in result.items() if k != "content_digest"},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    if len(encoded.encode()) > 512 * 1024 or result.get("content_digest") != digest(
        encoded
    ):
        raise ValueError("OPTIONAL_REVIEW_INVALID")
    return result


def _validated_bound_review(result, version, report):
    result = _validated_review(result, version)
    binding = {
        "scope_key": report["scope_key"],
        "account_environment": report["account_environment"],
        "as_of_ms": report["as_of_ms"],
        "window_start_ms": report["window"]["start_ms"],
        "window_end_ms": report["window"]["end_ms"],
    }
    if any(result.get(k) != v for k, v in binding.items()):
        raise ValueError("OPTIONAL_REVIEW_BINDING_INVALID")
    if version == FROZEN_REPLAY_VERSION:
        evidence = report.get("account_evidence")
        cutoff = evidence.get("as_of_ms") if isinstance(evidence, dict) else None
        if result.get("evidence_as_of_ms") != cutoff:
            raise ValueError("OPTIONAL_REVIEW_BINDING_INVALID")
    return result


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
        catalog_loader=load_execution_catalog_snapshot,
        account_day_loader=load_account_day_review,
        frozen_replay_loader=load_frozen_replay_review,
        setup_study_loader=load_setup_study,
        setup_path_reviewer=enrich_setup_paths,
    ):
        self.store, self.market, self.clock = store, market, clock_ms
        self.evidence_loader, self.notifier = evidence_loader, notifier
        self.path_reader, self.case_loader = path_reader, case_loader
        if type(force_evidence_refresh) is not bool:
            raise ValueError("MOVER_REFRESH_FLAG_INVALID")
        self.force_evidence_refresh = force_evidence_refresh
        self.flow_reviewer = flow_reviewer
        self.flow_evaluator = flow_evaluator
        self.catalog_loader = catalog_loader
        self.account_day_loader = account_day_loader
        self.frozen_replay_loader = frozen_replay_loader
        self.setup_study_loader = setup_study_loader
        self.setup_path_reviewer = setup_path_reviewer

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
        # Rendering is pure and has not sent anything. A formatting failure must
        # not consume the day's delivery claim or become an ambiguous send.
        try:
            text = format_digest(report, account_alias=report.get("account_alias"))
        except Exception:  # noqa: BLE001 - fixed diagnostics, never raw report data
            return "FORMAT_UNAVAILABLE_PENDING"
        if not self.store.claim_delivery(identity):
            return "ALREADY_HANDLED_OR_DELIVERY_UNKNOWN"
        try:
            message = self.notifier(text)
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
        catalog_snapshots = {}
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
                and existing[1].get("review_components") == REVIEW_COMPONENTS
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
            if scope.environment not in catalog_snapshots:
                # Separate read-only transaction: a missing optional grant or
                # stale catalog must never abort financial evidence acquisition.
                try:
                    with self.store.connect() as c:
                        c.execute("SET TRANSACTION READ ONLY")
                        catalog_snapshots[scope.environment] = self.catalog_loader(
                            c,
                            environment=scope.environment,
                            symbols=targets,
                            as_of_ms=as_of,
                        )
                except Exception:  # noqa: BLE001 - fixed optional diagnostic only
                    catalog_snapshots[scope.environment] = unavailable_snapshot(
                        environment=scope.environment,
                        symbols=targets,
                        as_of_ms=as_of,
                        reason="CATALOG_READ_UNAVAILABLE",
                    )
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
            report["review_components"] = dict(REVIEW_COMPONENTS)
            try:
                report["coverage_review"] = _validated_review(
                    build_coverage_review(report, catalog_snapshots[scope.environment]),
                    COVERAGE_VERSION,
                )
            except Exception:  # noqa: BLE001 - preserve the main report and delivery
                report["coverage_review"] = _unavailable_review(
                    COVERAGE_VERSION, "COVERAGE_REVIEW_UNAVAILABLE"
                )
            try:
                report["execution_review"] = _validated_review(
                    build_execution_review(report), EXECUTION_REVIEW_VERSION
                )
            except Exception:  # noqa: BLE001 - no model/analysis controls trading
                report["execution_review"] = _unavailable_review(
                    EXECUTION_REVIEW_VERSION, "EXECUTION_REVIEW_UNAVAILABLE"
                )
            # These bounded reads are independent of leaderboard coverage and
            # each other. In particular an empty ranking must not erase losses
            # from trades outside the ranking, or abort the other analysis.
            for key, version, loader, kwargs in (
                (
                    "account_day_review",
                    ACCOUNT_DAY_VERSION,
                    self.account_day_loader,
                    {"start_ms": window["start_ms"], "end_ms": window["end_ms"]},
                ),
                (
                    "frozen_replay_review",
                    FROZEN_REPLAY_VERSION,
                    self.frozen_replay_loader,
                    {},
                ),
                (
                    "setup_study",
                    SETUP_STUDY_VERSION,
                    self.setup_study_loader,
                    {"prior": existing[1].get("setup_study") if existing else None},
                ),
            ):
                try:
                    with self.store.connect() as c:
                        c.execute(
                            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                        )
                        result = loader(
                            c, scope, report=report, as_of_ms=as_of, **kwargs
                        )
                    report[key] = _validated_bound_review(result, version, report)
                except Exception:  # noqa: BLE001 - never block reports/trading or leak inputs
                    report[key] = _unavailable_review(
                        version, "REVIEW_READ_UNAVAILABLE"
                    )
            # Archive I/O happens after the PG transaction has closed, never in
            # execution or while holding a trading/account lock.
            try:
                paths_review = self.setup_path_reviewer(
                    report["setup_study"], self.path_reader, as_of_ms=as_of
                )
                paths_review = _validated_bound_review(
                    paths_review, SETUP_PATH_VERSION, report
                )
                for key, source in (
                    ("study_content_digest", "content_digest"),
                    ("input_digest", "input_digest"),
                    ("plan_digest", "plan_digest"),
                ):
                    if paths_review.get(key) != report["setup_study"].get(source):
                        raise ValueError("SETUP_PATH_BINDING_INVALID")
                report["setup_path_review"] = paths_review
            except Exception:  # noqa: BLE001 - independent diagnostic, no raw archive details
                report["setup_path_review"] = _unavailable_review(
                    SETUP_PATH_VERSION, "SETUP_PATH_UNAVAILABLE"
                )
            # An optional read may recover after a transient failure. Retry a
            # bounded number of times without resending a delivered daily DM.
            previous_attempts = (
                existing[1].get("optional_review_snapshot", {}).get("attempts", 0)
                if existing
                and existing[1].get("review_components") == REVIEW_COMPONENTS
                else 0
            )
            if type(previous_attempts) is not int or not 0 <= previous_attempts <= 3:
                previous_attempts = 0
            attempts = min(previous_attempts + 1, 3)
            jev = report["account_day_review"].get("jev_participation")
            jev_coverage = jev.get("coverage") if isinstance(jev, dict) else None
            jev_retryable = (
                isinstance(jev_coverage, dict) and jev_coverage.get("retryable") is True
            )
            optional_failed = (
                report["coverage_review"].get("status") not in {"OBSERVED_BOUNDED"}
                or report["execution_review"].get("status") == "UNKNOWN"
                or jev_retryable
                or any(
                    report[key].get("coverage", {}).get("retryable") is True
                    for key in (
                        "account_day_review",
                        "frozen_replay_review",
                        "setup_study",
                        "setup_path_review",
                    )
                )
            )
            optional_retry = (
                as_of + 3600000 if optional_failed and attempts < 3 else None
            )
            report["optional_review_snapshot"] = {
                "attempts": attempts,
                "status": "PARTIAL_UNKNOWN" if optional_failed else "COMPLETED",
                "retry_after_ms": optional_retry,
            }
            report["settlement_refresh"] = refresh_snapshot(
                report, previous=existing[1] if existing else None
            )
            settlement_retry = report["settlement_refresh"]["retry_after_ms"]
            if settlement_retry is not None:
                optional_retry = (
                    min(optional_retry, settlement_retry)
                    if optional_retry is not None
                    else settlement_retry
                )
            if optional_retry is not None:
                existing_retry = report["evidence_snapshot"]["retry_after_ms"]
                report["evidence_snapshot"]["retry_after_ms"] = (
                    min(existing_retry, optional_retry)
                    if existing_retry is not None
                    else optional_retry
                )
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
        days = [first + timedelta(days=n) for n in range((target - first).days + 1)]
        if hasattr(self.store, "pending_settlement_days"):
            try:
                pending = self.store.pending_settlement_days(last["date"], self.clock())
                if not isinstance(pending, list) or len(pending) > 3:
                    raise ValueError("PENDING_DAY_BOUND")
                pending = [date.fromisoformat(value) for value in pending]
                if any(
                    not target - timedelta(days=3) <= value < target
                    for value in pending
                ):
                    raise ValueError("PENDING_DAY_WINDOW")
                days += sorted(set(pending) - set(days), reverse=True)
            except Exception:  # noqa: BLE001 - historical research never blocks the new day
                emit(
                    component="daily-mover-review",
                    status="PENDING_DAY_READ_UNAVAILABLE",
                )
        for current in days:
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
