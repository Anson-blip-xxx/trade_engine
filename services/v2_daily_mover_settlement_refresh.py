"""Bounded research-only late settlement refresh, independent of read retries.

This is report scheduling, never settlement authority. Known pending facts may
be retried for 72 hours after the reviewed natural day, including across midnight.
The deadline cannot slide with retries; expired coverage remains unknown.
"""

from v2_core.daily_movers import DAY

VERSION = "daily-mover-settlement-refresh-v1"
INTERVAL_MS = 3_600_000
WINDOW_MS = 3 * DAY
PENDING_STATES = frozenset(
    {
        "AWAITING_TERMINAL_ORDERS",
        "AWAITING_SETTLEMENT",
        "SETTLEMENT_EVIDENCE_INCOMPLETE",
        "SETTLEMENT_REVISION_CHANGED",
    }
)


def refresh_snapshot(report, *, previous=None):
    """Called only after the account review has passed its digest/binding check.

    A failed read may retain a previous known wait, but cannot invent one. The
    parent report carries this snapshot inside its sealed content digest.
    """
    end, now = report["window"]["end_ms"], report["as_of_ms"]
    result = {
        "version": VERSION,
        "status": "NOT_APPLICABLE",
        "scope_key": report["scope_key"],
        "account_environment": report["account_environment"],
        "window_start_ms": report["window"]["start_ms"],
        "window_end_ms": end,
        "observed_at_ms": now,
        "deadline_ms": end + WINDOW_MS,
        "pending_count": 0,
        "pending_reasons": {},
        "retry_after_ms": None,
        "execution_authorized": False,
        "automatic_parameter_updates": False,
    }
    review = report.get("account_day_review", {})
    coverage = review.get("coverage", {})
    if (
        review.get("status") == "AVAILABLE"
        and coverage.get("query_complete") is True
        and coverage.get("full_closed_settlement_population_proven") is True
    ):
        result["status"] = "COMPLETE"
        return result
    reasons = coverage.get("excluded_reasons", {})
    pending = (
        {
            key: value
            for key, value in reasons.items()
            if key in PENDING_STATES and type(value) is int and 0 < value <= 500
        }
        if isinstance(reasons, dict)
        else {}
    )
    # Explicit settlement labels remain facts even if another episode's read
    # failed. A partial read alone cannot invent pending settlement, however.
    if 0 < sum(pending.values()) <= 500:
        result.update(
            status="WAITING_SETTLEMENT"
            if coverage.get("query_complete") is True
            else "WAITING_EVIDENCE",
            pending_reasons=pending,
            pending_count=sum(pending.values()),
        )
    elif coverage.get("query_complete") is not True and isinstance(previous, dict):
        prior = previous.get("settlement_refresh", {})
        if (
            isinstance(prior, dict)
            and prior.get("version") == VERSION
            and prior.get("status") in {"WAITING_SETTLEMENT", "WAITING_EVIDENCE"}
            and all(
                prior.get(k) == result[k]
                for k in (
                    "scope_key",
                    "account_environment",
                    "window_start_ms",
                    "window_end_ms",
                    "deadline_ms",
                )
            )
            and type(prior.get("observed_at_ms")) is int
            and prior["observed_at_ms"] <= now
            and type(prior.get("pending_count")) is int
            and 0 < prior["pending_count"] <= 500
            and isinstance(prior.get("pending_reasons"), dict)
            and all(
                k in PENDING_STATES and type(v) is int and 0 < v <= 500
                for k, v in prior["pending_reasons"].items()
            )
            and sum(prior["pending_reasons"].values()) == prior["pending_count"]
        ):
            result.update(
                status="WAITING_EVIDENCE",
                pending_count=prior["pending_count"],
                pending_reasons=dict(prior["pending_reasons"]),
            )
    if result["pending_count"]:
        if now >= result["deadline_ms"]:
            result["status"] = "WINDOW_CLOSED_UNRESOLVED"
        else:
            result["retry_after_ms"] = min(now + INTERVAL_MS, result["deadline_ms"])
    return result
