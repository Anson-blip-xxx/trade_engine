"""Durable S3 candle evidence -> strict global V2 market regime.

PostgreSQL selects the authoritative acknowledged delivery, ClickHouse supplies
the content-addressed source payload, and Redis receives only the derived current
projection. Missing inputs never become neutral defaults.
"""

import json
from decimal import Decimal, InvalidOperation

from s0.core import classify_regime
from services.v2_market_publishers import S0Publisher
from services.v2_s3_candles import VERSION as S3_VERSION
from services.v2_s3_candles import build_frame
from services.v2_s3_source import frame_digest
from v2_core.evidence import digest
from v2_core.ingress import symbol

VERSION = "s0-regime-s3-v1"


def _number(value, *, positive=False):
    if type(value) not in (str, int) or len(str(value)) > 80:
        raise ValueError("bounded S3 decimal required")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("valid S3 decimal required") from exc
    if (
        not result.is_finite()
        or abs(result) > Decimal("1e20")
        or (positive and result <= 0)
    ):
        raise ValueError("S3 decimal outside regime bounds")
    return result


def build_s0_state(windows, *, universe, s3_frame_id, s3_input_digest):
    """Compute the legacy-compatible price/breadth regime without fallbacks."""
    if not isinstance(universe, (list, tuple)) or not universe:
        raise ValueError("explicit S0 universe required")
    expected = tuple(sorted(symbol(item) for item in universe))
    if len(set(expected)) != len(expected) or "BTCUSDT" not in expected:
        raise ValueError("unique S0 universe including BTCUSDT required")
    if not isinstance(windows, dict) or set(windows) != set(expected):
        raise ValueError("complete S0 universe required")
    if not isinstance(s3_frame_id, str) or not isinstance(s3_input_digest, str):
        raise TypeError("durable S3 evidence identity required")

    closed_at = None
    for target in expected:
        context = windows[target]
        if not isinstance(context, dict) or not all(
            isinstance(context.get(period), dict)
            for period in ("15m", "1h", "4h", "24h")
        ):
            raise ValueError("complete S3 feature windows required")
        evidence = context.get("input_evidence")
        if (
            not isinstance(evidence, dict)
            or evidence.get("source") != "BINANCE_FUTURES"
            or evidence.get("window_version") != S3_VERSION
            or evidence.get("interval") != "1m"
            or evidence.get("bar_count") != 1440
            or type(evidence.get("closed_at")) is not int
        ):
            raise ValueError("verified S3 input evidence required")
        closed_at = evidence["closed_at"] if closed_at is None else closed_at
        if evidence["closed_at"] != closed_at:
            raise ValueError("mixed S3 observation times")

    btc = windows["BTCUSDT"]
    four_hour, fifteen, day = btc["4h"], btc["15m"], btc["24h"]
    ema20 = _number(four_hour.get("ema20"), positive=True)
    ema60 = _number(four_hour.get("ema60"), positive=True)
    price = _number(four_hour.get("close"), positive=True)
    high = _number(fifteen.get("high"), positive=True)
    low = _number(fifteen.get("low"), positive=True)
    if low > high:
        raise ValueError("invalid S3 price range")
    vol_15m = _number(fifteen.get("volatility"))
    vol_24h = _number(day.get("volatility"))
    if vol_15m < 0 or vol_24h < 0:
        raise ValueError("negative S3 volatility")

    if ema20 > ema60 and price > ema20:
        btc_trend = "bull"
    elif ema20 < ema60 and price < ema20:
        btc_trend = "bear"
    else:
        btc_trend = "neutral"
    amp = (high - low) / price
    volatility = (
        "low"
        if amp < Decimal("0.015")
        else ("normal" if amp < Decimal("0.03") else "high")
    )
    atr_expanding = vol_24h > 0 and vol_15m > vol_24h * Decimal("1.3")
    btc_below_ema60 = price < ema60

    above = 0
    for target in expected:
        one_hour = windows[target]["1h"]
        close = _number(one_hour.get("close"), positive=True)
        breadth_ema = _number(one_hour.get("ema20"), positive=True)
        above += int(close > breadth_ema)
    breadth_ratio = Decimal(above) / Decimal(len(expected))
    breadth = (
        "strong"
        if breadth_ratio > Decimal("0.7")
        else ("normal" if breadth_ratio > Decimal("0.4") else "weak")
    )

    state = classify_regime(
        btc_trend,
        volatility,
        float(amp),
        btc_below_ema60,
        atr_expanding,
        breadth,
        float(breadth_ratio),
    )
    # Optional legacy feeds are absent, not fabricated as neutral observations.
    for key in (
        "fng",
        "fng_label",
        "avg_funding",
        "sentiment_risk",
        "sentiment_bias",
        "alts_sync",
        "shock_score",
    ):
        state.pop(key)
    return {
        **state,
        "btc_amp": float(amp),
        "btc_below_ema60": btc_below_ema60,
        "atr_expanding": atr_expanding,
        "supplemental_inputs": "NOT_CONFIGURED",
        "evidence": {
            "algorithm": VERSION,
            "s3_frame_id": s3_frame_id,
            "s3_input_digest": s3_input_digest,
            "closed_at": closed_at,
            "universe": list(expected),
        },
    }


class ArchivedS0Stage:
    """One fail-closed, replay-safe global regime projection."""

    def __init__(self, connect, *, publisher, archive, universe):
        if publisher.publisher.source != "s0" or not callable(
            getattr(archive, "get", None)
        ):
            raise ValueError("bound S0 publisher and archive required")
        self._connect, self.publisher, self.archive = connect, publisher, archive
        self.environment = publisher.publisher.environment
        self.universe = tuple(sorted(symbol(item) for item in universe))
        if (
            len(set(self.universe)) != len(self.universe)
            or "BTCUSDT" not in self.universe
        ):
            raise ValueError("unique S0 universe including BTCUSDT required")

    def _head(self):
        with self._connect() as conn:
            return conn.execute(
                """SELECT d.frame_id,d.observed_at_ms,d.archive_digest,d.input_digest,
                d.status,f.input_digest,
                EXISTS(SELECT 1 FROM v2_candle_delivery_events e
                    WHERE e.environment=d.environment AND e.frame_id=d.frame_id
                    AND e.outcome='ACKNOWLEDGED')
                FROM v2_candle_deliveries d LEFT JOIN v2_s3_frames f
                ON f.environment=d.environment AND f.frame_id=d.frame_id
                WHERE d.environment=%s
                ORDER BY d.observed_at_ms DESC,d.frame_id DESC LIMIT 1""",
                (self.environment,),
            ).fetchone()

    def run_once(self):
        head = self._head()
        if head is None:
            raise ValueError("S0_SOURCE_MISSING")
        frame_id, observed, archive_hash, input_hash, status, published, ack = head
        if status != "ACKNOWLEDGED" or published != input_hash or not ack:
            raise ValueError("S0_SOURCE_NOT_CONFIRMED")
        encoded = self.archive.get(archive_hash)
        if (
            not isinstance(encoded, str)
            or len(encoded.encode()) > 8_000_000
            or digest(encoded) != archive_hash
        ):
            raise ValueError("S0_ARCHIVE_UNAVAILABLE")
        try:
            batch = json.loads(encoded)
            frame = build_frame(batch, environment=self.environment)
        except (ValueError, TypeError, KeyError, RecursionError):
            raise ValueError("S0_ARCHIVE_INPUT_MISMATCH") from None
        if (
            frame["frame_id"] != frame_id
            or frame["observed_at"] != observed
            or frame_digest(frame) != input_hash
        ):
            raise ValueError("S0_SOURCE_RECEIPT_MISMATCH")
        state = build_s0_state(
            frame["windows"],
            universe=self.universe,
            s3_frame_id=frame_id,
            s3_input_digest=input_hash,
        )
        if self._head() != head:
            raise ValueError("S0_SOURCE_SUPERSEDED")
        result = self.publisher.publish_state(
            state,
            frame_id=f"{VERSION}:{observed}",
            observed_at=observed,
        )
        if (
            result.get("status") != "RECORDED"
            or result.get("market_status") != "PROJECTED"
        ):
            return {"status": "RETRY", "stage": "PROJECT", "frame_id": frame_id}
        return {"status": "PROJECTED", "frame_id": frame_id}


def create_s0_stage(
    connect,
    *,
    archive,
    redis_client,
    environment,
    symbols,
    clock_ms,
    max_age_ms,
    lifetime_ms,
):
    """Explicit composition only; construction performs no I/O."""
    from v2_core.producer import ProducerPublisher, RedisMarketContext

    publisher = ProducerPublisher(
        connect,
        source="s0",
        environment=environment,
        market=RedisMarketContext(redis_client, environment=environment),
        clock_ms=clock_ms,
        max_age_ms=max_age_ms,
        lifetime_ms=lifetime_ms,
    )
    return ArchivedS0Stage(
        connect,
        publisher=S0Publisher(publisher),
        archive=archive,
        universe=symbols,
    )
