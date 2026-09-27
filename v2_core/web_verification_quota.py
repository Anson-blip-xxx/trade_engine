"""Web-only fixed-cost Testnet verification permit, without budget table access."""


class WebVerificationPermit:
    def __init__(self, connect):
        self.connect = connect

    def __call__(self, method, path):
        if (method, path) != ("GET", "/fapi/v1/positionSide/dual"):
            return False
        # Own committed transaction BEFORE network I/O. A failed probe or outer
        # verification transaction cannot refund the already consumed allowance.
        with self.connect() as c:
            return (
                c.execute("SELECT v2_web_testnet_verify_permit()").fetchone()[0] is True
            )

    def penalize(self, seconds):
        if type(seconds) is not int or not 1 <= seconds <= 259200:
            raise ValueError("EGRESS_COOLDOWN_INVALID")
        with self.connect() as c:
            c.execute("SELECT v2_web_testnet_quota_cooldown(%s)", (seconds,))
