"""Optional background JEV reference attached to frozen entry evidence.

No signed exchange calls, balances, positions or order capabilities. The queue
is disposable advisory work; durable claims, budgets and results live in PG.
Main-thread lookup never calls the model or waits for the background worker.
"""

import json
import queue
import threading
from collections import OrderedDict
from contextlib import nullcontext
from copy import deepcopy
from decimal import Context, Decimal, localcontext

from services.v2_jev_coordinator import CONTROL_KEY
from services.v2_jev_transport import SharedJevQuery
from v2_core.account_risk import AccountScope
from v2_core.jev_advisory import (
    _NUMBERS,
    STRUCTURE_INDICATOR_CONTRACT,
    STRUCTURE_INDICATOR_VERSION,
    AdvisoryPolicy,
    JevAdvisory,
    _cached,
    prepare_request,
)
from v2_core.producer import market_envelope
from v2_core.state import BusinessState

PUBLIC_SCOPE = AccountScope("BINANCE", "market-advisory", "SANDBOX", "FUTURES")
CONTROL_MAX_AGE_MS = 5000
LOCAL_CACHE_LIMIT = 128


def public_features(envelope, *, target, direction, context=None, now_ms=None):
    """Select measured candle/funding/spread facts, never the full context.

    Supplemental facts require their recorded source clocks; missing or stale
    measurements remain absent. No private balances, keys or account IDs leave.
    """
    if (
        type(envelope) is not dict
        or envelope.get("source") != "s3"
        or envelope.get("environment") != "SANDBOX"
        or envelope.get("symbol") != target
        or envelope
        != market_envelope(
            "s3", "SANDBOX", target, envelope["observed_at"], envelope["features"]
        )
    ):
        raise ValueError("JEV_PUBLIC_ENVELOPE_INVALID")
    frames = envelope["features"]
    short, hour = frames.get("15m", {}), frames.get("1h", {})
    input_evidence = frames.get("input_evidence")
    structure = None
    if (
        type(input_evidence) is dict
        and input_evidence.get("window_version") == STRUCTURE_INDICATOR_VERSION
    ):
        from v2_core.trend_structure import validate_trend_structure

        if (
            input_evidence.get("source") != "BINANCE_FUTURES"
            or input_evidence.get("environment") != "SANDBOX"
            or input_evidence.get("interval") != "1m"
            or type(input_evidence.get("closed_at")) is not int
            or input_evidence["closed_at"] != envelope["observed_at"]
        ):
            raise ValueError("JEV_PUBLIC_INDICATOR_EVIDENCE_INVALID")
        structure = validate_trend_structure(
            frames.get("trend_structure"),
            symbol=target,
            environment="SANDBOX",
            closed_at_ms=envelope["observed_at"],
        )
        if structure["status"] != "READY":
            raise ValueError("JEV_PUBLIC_INDICATOR_WARMUP")
        if context is not None and (
            type(context) is not dict
            or type(context.get("directional")) is not dict
            or context["directional"].get("market") != frames
        ):
            raise ValueError("JEV_PUBLIC_INDICATOR_CONTEXT_MISMATCH")

    def number(value):
        if type(value) not in (str, int) or len(str(value)) > 80:
            raise ValueError("JEV_PUBLIC_NUMBER_INVALID")
        try:
            parsed = Decimal(value)
        except ArithmeticError:
            raise ValueError("JEV_PUBLIC_NUMBER_INVALID") from None
        if (
            not parsed.is_finite()
            or parsed.copy_abs() > Decimal("1e12")
            or not -64 <= parsed.as_tuple().exponent <= 64
        ):
            raise ValueError("JEV_PUBLIC_NUMBER_INVALID")
        return parsed

    def normalized(field, value):
        if value is None:
            return None
        parsed = value if type(value) is Decimal else number(value)
        low, high = map(Decimal, _NUMBERS[field])
        # Validate the actual observation before rounding. A source RSI above
        # 100 or negative ATR must not become an apparently valid boundary.
        if not parsed.is_finite() or not low <= parsed <= high:
            raise ValueError("JEV_PUBLIC_NUMBER_INVALID")
        with localcontext(Context(prec=50)):
            rounded = parsed.quantize(Decimal("0.000000000001"))
        if not low <= rounded <= high:
            raise ValueError("JEV_PUBLIC_NUMBER_INVALID")
        return "0" if rounded == 0 else format(rounded, "f").rstrip("0").rstrip(".")

    def gap(frame):
        if frame.get("close") is None or frame.get("ema20") is None:
            return None
        with localcontext(Context(prec=50)):
            close, ema = number(frame["close"]), number(frame["ema20"])
            if close <= 0 or ema <= 0:
                raise ValueError("JEV_PUBLIC_NUMBER_INVALID")
            return (close / ema - 1) * 100

    values = {
        "symbol": target,
        "observed_at_ms": envelope["observed_at"],
        "timeframe": "15m",
        "direction": direction,
        "price": short.get("close"),
        "return_15m_pct": short.get("chg"),
        "return_1h_pct": hour.get("chg"),
        "return_4h_pct": frames.get("4h", {}).get("chg"),
        # Candle-only base. Measured supplemental funding/spread may follow;
        # never infer depth, OI, volume ratios or private account data.
    }
    if structure is None:
        # Preserve legacy public state, questions and cache identity byte-for-
        # byte. Only the explicitly selected v6 contract changes field units.
        values.update(
            atr_pct=short.get("atr_pct"),
            rsi_15m=short.get("rsi"),
            ema_gap_15m_pct=gap(short),
            ema_gap_1h_pct=gap(hour),
            taker_buy_ratio=short.get("taker_buy_ratio"),
        )
    else:
        measured = structure["values"]
        values.update(
            atr_pct=measured["atr_pct"],
            rsi_15m=None,
            ema_gap_15m_pct=gap(measured),
            ema_gap_1h_pct=None,
            taker_buy_ratio=measured["taker_buy_ratio"],
            relative_volume_15m=measured["volume_ratio"],
            indicator_version=STRUCTURE_INDICATOR_VERSION,
            indicator_contract=STRUCTURE_INDICATOR_CONTRACT,
            indicator_closed_at_ms=structure["last_closed_at_ms"],
        )
    if context is not None:
        # The full context never enters the queue. Select only proven public
        # scalars already obtained by normal admission; no extra HTTP reads.
        if (
            type(context) is not dict
            or context.get("symbol") != target
            or context.get("environment") != "SANDBOX"
            or type(now_ms) is not int
            or type(context.get("assembled_at")) is not int
            or type(context.get("valid_until_ms")) is not int
            or not context["assembled_at"] <= now_ms < context["valid_until_ms"]
        ):
            raise ValueError("JEV_PUBLIC_CONTEXT_INVALID")
        directional = context.get("directional", {})
        evidence = context.get("account_evidence", {})
        funding_at = evidence.get("sizing_mark_observed_at_ms")
        if (
            type(funding_at) is int
            and 0 <= now_ms - funding_at < 15000
            and directional.get("funding_rate") is not None
        ):
            values.update(
                funding_rate=directional["funding_rate"],
                funding_observed_at_ms=funding_at,
            )
        venue = directional.get("leverage_venue")
        if type(venue) is dict and venue.get("symbol") == target:
            stamp = venue.get("book_at_ms")
            if (
                type(stamp) is int
                and 0 <= now_ms - stamp < 15000
                and venue.get("spread_fraction") is not None
            ):
                values.update(
                    spread_bps=number(venue["spread_fraction"]) * 10000,
                    spread_observed_at_ms=stamp,
                )
    # Public producers may emit long decimal or scientific notation. Normalize
    # only this explicit whitelist; the core request validator remains strict.
    return {
        key: normalized(key, value) if key in _NUMBERS else value
        for key, value in values.items()
    }


def unavailable(reason="REFERENCE_UNAVAILABLE", *, status="FALLBACK"):
    return {
        "status": status,
        "reason": reason,
        "decision_effect": "NONE",
        "execution_authorized": False,
        "risk_override": False,
    }


class JevReference:
    def __init__(self, connect, *, clock_ms, stop):
        self.connect, self.clock, self.stop = connect, clock_ms, stop
        self.queue = queue.Queue(maxsize=8)
        self.lock = threading.Lock()
        self.pending = set()
        self.thread = None
        # Disposable copies only. PG remains authoritative; losing every item
        # on restart is safe. No main-thread DB, Redis, HTTP or filesystem read.
        self.cache = OrderedDict()
        self.control = None

    def policy(self):
        with self.connect() as conn:
            conn.execute("SET LOCAL lock_timeout='50ms'")
            conn.execute("SET LOCAL statement_timeout='100ms'")
            row = BusinessState(lambda: nullcontext(conn)).read(CONTROL_KEY)
        if row is None or row.deleted:
            return AdvisoryPolicy()
        return AdvisoryPolicy(**json.loads(row.payload_json))

    def advisor(self, policy):
        from services.v2_jev_coordinator import JevCoordinator

        return JevAdvisory(
            JevCoordinator(
                self.connect,
                scope=PUBLIC_SCOPE,
                enabled=policy.enabled,
                requests_per_minute=5,
                tokens_per_day=500000,
                max_age_ms=policy.max_age_ms,
                cache_ttl_ms=policy.cache_ttl_ms,
                token_budget_pacing=policy.token_budget_pacing,
                token_pacing_burst_tokens=policy.token_pacing_burst_tokens,
            ),
            SharedJevQuery(),
            policy=policy,
            clock_ms=self.clock,
        )

    def submit(self, features):
        try:
            if self.stop.is_set():
                return
            now = self.clock()
            # Admission to this disposable queue is not API permission. The
            # worker must use its fresh authoritative policy before evaluate.
            prepared = prepare_request(features, now_ms=now, max_age_ms=300000)
            key = prepared["request_key"]
            if not self.lock.acquire(blocking=False):
                return
            try:
                if self.stop.is_set():
                    return
                fresh = self._control_fresh(self.control, now)
                if fresh and not self.control[0].enabled:
                    return
                if fresh and key in self.cache:
                    policy, cached = self.cache[key]
                    if policy == self.control[0] and now < cached["expires_at_ms"]:
                        return
                if key in self.pending:
                    self._start_worker()
                    return
                try:
                    # Store only validated public scalars, never a caller's
                    # original context, references or unknown extra fields.
                    self.queue.put_nowait((key, prepared["request"]["state"]))
                except queue.Full:
                    self._start_worker()
                    return
                self.pending.add(key)
                self._start_worker()
            finally:
                self.lock.release()
        except Exception:  # noqa: BLE001 - optional work must not break admission
            return

    def _start_worker(self):
        # Called only under the short in-memory lock. A failed lazy startup can
        # be retried on the next identical submission without duplicating work.
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(
                target=self._run, name="jev-reference", daemon=True
            )
            self.thread.start()

    @staticmethod
    def _control_fresh(control, now):
        return (
            control is not None
            and isinstance(control[0], AdvisoryPolicy)
            and type(now) is int
            and type(control[1]) is int
            and 0 <= now - control[1] < CONTROL_MAX_AGE_MS
        )

    def _remember_control(self, policy):
        observed = self.clock()
        if not isinstance(policy, AdvisoryPolicy) or type(observed) is not int:
            raise ValueError("JEV_CONTROL_INVALID")
        with self.lock:
            if self.control is None or self.control[0] != policy:
                self.cache.clear()
            self.control = (policy, observed)

    def _forget_control(self):
        with self.lock:
            self.control = None
            self.cache.clear()

    def _run(self):
        while not self.stop.is_set():
            try:
                key, features = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                # Kill switch is reread before any external model request.
                policy = self.policy()
                self._remember_control(policy)
                if policy.enabled and not self.stop.is_set():
                    prepared = prepare_request(
                        features, now_ms=self.clock(), max_age_ms=policy.max_age_ms
                    )
                    if prepared["request_key"] != key:
                        raise ValueError("JEV_QUEUE_IDENTITY_CHANGED")
                    # JevAdvisory returns READY only after coordinator.complete
                    # positively confirms durable CAS publication. Never query
                    # the provider or reconstitute a result in the main thread.
                    result = self.advisor(policy).evaluate(features)
                    if result.get("status") == "ADVISORY_READY":
                        now = self.clock()
                        checked = _cached(result, prepared, now, policy.cache_ttl_ms)
                        with self.lock:
                            if (
                                not self.stop.is_set()
                                and self._control_fresh(self.control, now)
                                and self.control[0] == policy
                            ):
                                self.cache[key] = (policy, checked)
                                self.cache.move_to_end(key)
                                while len(self.cache) > LOCAL_CACHE_LIMIT:
                                    self.cache.popitem(last=False)
            except Exception:  # noqa: BLE001 - no provider/DB failure reaches the caller
                self._forget_control()
            finally:
                with self.lock:
                    self.pending.discard(key)
                self.queue.task_done()

    def lookup(self, features):
        try:
            if self.stop.is_set():
                return unavailable("STOPPED", status="OFF")
            now = self.clock()
            if not self.lock.acquire(blocking=False):
                return unavailable("LOCAL_CACHE_BUSY")
            try:
                control = self.control
                if control is None:
                    return unavailable("CONTROL_UNAVAILABLE", status="OFF")
                if not self._control_fresh(control, now):
                    return unavailable("CONTROL_STALE")
                policy = control[0]
                if not policy.enabled:
                    return unavailable("DISABLED", status="OFF")
                prepared = prepare_request(
                    features, now_ms=now, max_age_ms=policy.max_age_ms
                )
                cached = self.cache.get(prepared["request_key"])
                if cached is None or cached[0] != policy:
                    return unavailable("CACHE_MISS")
                result = deepcopy(cached[1])
            finally:
                self.lock.release()
            checked_now = self.clock()
            if not self._control_fresh(control, checked_now):
                return unavailable("CONTROL_STALE")
            checked = _cached(result, prepared, checked_now, policy.cache_ttl_ms)
            if not self.lock.acquire(blocking=False):
                return unavailable("LOCAL_CACHE_BUSY")
            try:
                if (
                    self.stop.is_set()
                    or not self._control_fresh(self.control, checked_now)
                    or self.control[0] != policy
                ):
                    return unavailable("CONTROL_CHANGED")
            finally:
                self.lock.release()
            return checked
        except Exception:  # noqa: BLE001 - neutral fixed fallback, not a trade veto
            return unavailable()


class JevPrefetchMarketContext:
    """Reuse the original market read and opportunistically queue public work."""

    def __init__(self, delegate, *, reference, strategy):
        if (
            strategy not in {"S6", "S8"}
            or not callable(delegate)
            or not callable(getattr(reference, "submit", None))
        ):
            raise ValueError("JEV_REFERENCE_BINDING_REQUIRED")
        self.delegate, self.reference = delegate, reference
        self.direction = "LONG" if strategy == "S6" else "SHORT"

    def __call__(self, signal):
        context = self.delegate(signal)
        try:
            self.reference.submit(
                public_features(
                    context["sources"]["s3"],
                    target=signal["symbol"],
                    direction=self.direction,
                )
            )
        except Exception:  # noqa: BLE001, S110 - no model dependency for normal context
            pass
        return context


class JevReferenceContext:
    def __init__(self, delegate, *, reference, strategy):
        if (
            strategy not in {"S6", "S8"}
            or not callable(delegate)
            or not callable(getattr(reference, "lookup", None))
        ):
            raise ValueError("JEV_REFERENCE_BINDING_REQUIRED")
        self.delegate, self.reference = delegate, reference
        self.direction = "LONG" if strategy == "S6" else "SHORT"

    def __call__(self, signal):
        # Original errors/gates/expiry are unchanged. In particular this wrapper
        # performs no extra market read and never calls evaluate or policy.
        context = self.delegate(signal)
        try:
            features = public_features(
                context["sources"]["s3"],
                target=signal["symbol"],
                direction=self.direction,
            )
            reference = self.reference.lookup(features)
        except Exception:  # noqa: BLE001
            reference = unavailable()
        return {**context, "jev_advisory": reference}
