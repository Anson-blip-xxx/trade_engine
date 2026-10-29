"""Explicit evidence that a submission has not reached the write transport."""


class SubmissionNotSent(ValueError):
    def __init__(self, reason):
        if reason not in {
            "VENUE_PREFLIGHT_UNAVAILABLE",
            "VENUE_MODE_UNSUPPORTED",
            "ENDPOINT_OR_WRITE_DISABLED",
            "QUOTA_DENIED",
            "EXCHANGE_CREATED_ORDER_QUERY_ONLY",
            "VENUE_READINESS_BLOCKED",
            "EXECUTION_FENCED",
            "EXECUTION_GUARD_PERMISSION_DENIED",
            "DIRECTIONAL_SUBMISSION_PRICE_INVALID",
            "DIRECTIONAL_SUBMISSION_STOP_INVALID",
            "DIRECTIONAL_SUBMISSION_PRICE_MOVED",
            "REENTRY_SUBMISSION_BLOCKED",
            "REDUCE_ONLY_REVALIDATION_FAILED",
        }:
            raise ValueError("unsupported no-submission evidence")
        super().__init__(
            reason
            + (
                ": one-way account mode required"
                if reason == "VENUE_MODE_UNSUPPORTED"
                else ""
            )
        )
        self.reason = reason
