"""Bounded read-only historical research adapter; never an execution dependency."""

import json
from datetime import date, datetime, time, timedelta, timezone

from v2_core.daily_movers import DAY
from v2_core.daily_movers import VERSION as MARKET_VERSION
from v2_core.evidence import canonical, digest
from v2_core.mover_flow_evaluation import VERSION, evaluate_flow_history

HISTORY_DAYS = 30
MAX_DAY_BYTES = 1024 * 1024
MAX_HISTORY_BYTES = 32 * 1024 * 1024
UTC8 = timezone(timedelta(hours=8))


def unavailable_evaluation(reason):
    report = {
        "version": VERSION,
        "status": "UNKNOWN",
        "reason": reason,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
        "promotion_authorized": False,
    }
    return {**report, "content_digest": digest(canonical(report))}


def _day(value):
    if not isinstance(value, str):
        raise ValueError("FLOW_EVALUATION_DAY_INVALID")  # noqa: TRY004 - fixed validation code
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value or parsed.year < 1970:
        raise ValueError("FLOW_EVALUATION_DAY_INVALID")
    return parsed


class FlowHistoryEvaluation:
    def __init__(self, connect):
        if not callable(connect):
            raise TypeError("FLOW_EVALUATION_CONNECTION_REQUIRED")
        self.connect = connect

    def history(self, day):
        """Latest revision per day across accounts, public research sections only.

        Unknown sections are retained. Never choose a revision based on its
        observed success, component coverage, or eventual outcome. A DB timeout
        fails this optional evaluation, not the primary report or trading.
        """
        _day(day)
        with self.connect() as connection:
            connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
            )
            connection.execute("SET LOCAL statement_timeout='3s'")
            connection.execute("SET LOCAL lock_timeout='100ms'")
            rows = connection.execute(
                """WITH latest AS (
                  SELECT DISTINCT ON(day) day,report
                  FROM v2_daily_mover_reports
                  WHERE method_version=%s AND day<%s::date
                    AND day>=%s::date-%s
                    AND report->>'kind' IS DISTINCT FROM 'DAILY_MOVER_JOB_FAILURE'
                  ORDER BY day DESC,created_at DESC,report_id DESC LIMIT %s
                ), projections AS (
                  SELECT day,jsonb_build_object(
                    'precursor_review',report->'precursor_review',
                    'flow_review',report->'flow_review')::text AS payload
                  FROM latest
                )
                SELECT day,octet_length(payload),
                  CASE WHEN octet_length(payload)<=%s THEN payload ELSE NULL END
                FROM projections ORDER BY day""",
                (MARKET_VERSION, day, day, HISTORY_DAYS, HISTORY_DAYS, MAX_DAY_BYTES),
            )
            result, total = [], 0
            for stored_day, size, encoded in rows:
                if (
                    type(stored_day) is not date
                    or not _day(day) - timedelta(days=HISTORY_DAYS)
                    <= stored_day
                    < _day(day)
                    or type(size) is not int
                    or size < 0
                    or len(result) >= HISTORY_DAYS
                ):
                    raise ValueError("FLOW_EVALUATION_HISTORY_INVALID")
                if encoded is None:
                    # Oversize remains a missing day, never silently dropped.
                    item = {
                        "precursor_review": None,
                        "flow_review": None,
                        "history_error": "DAY_PROJECTION_TOO_LARGE",
                    }
                else:
                    if (
                        not isinstance(encoded, str)
                        or len(encoded.encode()) != size
                        or size > MAX_DAY_BYTES
                    ):
                        raise ValueError("FLOW_EVALUATION_PROJECTION_INVALID")
                    total += size
                    if total > MAX_HISTORY_BYTES:
                        raise ValueError("FLOW_EVALUATION_HISTORY_TOO_LARGE")
                    item = json.loads(encoded)
                    if not isinstance(item, dict) or set(item) != {
                        "precursor_review",
                        "flow_review",
                    }:
                        raise ValueError("FLOW_EVALUATION_PROJECTION_INVALID")
                result.append({"day": stored_day.isoformat(), **item})
            return result

    def build(self, *, window, precursor, flow_review, as_of_ms):
        day = _day(window.get("date"))
        start = int(datetime.combine(day, time(), UTC8).timestamp()) * 1000
        if (
            type(window.get("start_ms")) is not int
            or type(window.get("end_ms")) is not int
            or window["start_ms"] != start
            or window["end_ms"] != start + DAY
            or type(as_of_ms) is not int
            or not start + DAY <= as_of_ms < 2**53
        ):
            raise ValueError("FLOW_EVALUATION_WINDOW_INVALID")
        current = {
            "day": day.isoformat(),
            "precursor_review": precursor,
            "flow_review": flow_review,
        }
        if len(canonical(current).encode()) > MAX_DAY_BYTES:
            raise ValueError("FLOW_EVALUATION_CURRENT_TOO_LARGE")
        report = evaluate_flow_history(
            [*self.history(day.isoformat()), current], as_of_ms=as_of_ms
        )
        report["history_read"] = {
            "status": "COMPLETED",
            "prior_calendar_days": HISTORY_DAYS,
            "current_day_included": True,
            "account_reports_deduplicated_by_day": True,
            "max_day_bytes": MAX_DAY_BYTES,
            "max_prior_history_bytes": MAX_HISTORY_BYTES,
            "latest_revision_basis": "CREATED_AT_THEN_REPORT_ID_NOT_OUTCOME_OR_COVERAGE",
            "operational_failure_notifications_excluded": True,
        }
        report.pop("content_digest", None)
        return {**report, "content_digest": digest(canonical(report))}
