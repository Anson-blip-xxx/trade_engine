"""Explicit evidence that a submission has not reached the write transport."""


class SubmissionNotSent(ValueError):
    def __init__(self, reason):
        if reason not in {
            "VENUE_PREFLIGHT_UNAVAILABLE",
            "VENUE_MODE_UNSUPPORTED",
            "ENDPOINT_OR_WRITE_DISABLED",
            "QUOTA_DENIED",
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
