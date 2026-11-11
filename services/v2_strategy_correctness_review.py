"""Pure, bounded same-input indicator/admission comparison, never a backtest.

The caller owns historical archive access. No DB, network, account identifiers,
policy writes, fees, simulated fills or PnL attribution are available here.
"""

from dataclasses import asdict

from services.v2_directional_replay import market_plan
from services.v2_s3_candles import CORRECTED_VERSION, PRICE_VERSION, build_frame
from services.v2_s3_runtime import evaluate
from v2_core.directional import supports_event
from v2_core.evidence import canonical, digest
from v2_core.producer import market_envelope
from v2_core.runtime_policy import resolve


def compare_frame(batch, *, regime, policy=None):
    """Compare both features and confirmation rules on one frozen closed frame.

    One symbol only, at most the fixed detector's output count. Generated event
    sets are reported separately. Admission probes use their union, with the
    original emitting version attached to every probe; do not confuse probes of
    absent events with orders the other version would actually have received.
    """
    if not isinstance(batch, dict) or len(batch.get("candles", {})) != 1:
        raise ValueError("CORRECTNESS_REVIEW_SINGLE_SYMBOL_REQUIRED")
    if regime not in {"strong_bull", "weak_bull", "neutral", "weak_bear", "risk-off", "risk_off"}:
        raise ValueError("CORRECTNESS_REVIEW_REGIME_REQUIRED")
    baseline = {**resolve(policy), "entry.confirmation_version": 1}
    corrected = {**baseline, "entry.confirmation_version": 2}
    frames, events = {}, {}
    target = next(iter(batch["candles"]))
    for version in (PRICE_VERSION, CORRECTED_VERSION):
        frame = build_frame(batch, environment="SANDBOX", feature_version=version)
        emitted, _ = evaluate(
            frame["windows"], frame["raw_windows"], {}, frame["observed_at"]
        )
        frames[version], events[version] = frame, emitted
    observed = frames[PRICE_VERSION]["observed_at"]
    probes = []
    for origin, generated in events.items():
        if len(generated) > 32:
            raise ValueError("CORRECTNESS_REVIEW_EVENT_BOUND")
        for event in generated:
            for strategy in ("S6", "S8"):
                if not supports_event(strategy, event["type"]):
                    continue
                signal = {
                    "symbol": target,
                    "signal": event["type"],
                    "features": event,
                    "observed_at": observed,
                    "expires_at_ms": observed + 30000,
                }
                variants = {}
                for label, version, settings in (
                    ("BASELINE", PRICE_VERSION, baseline),
                    ("INDICATORS_ONLY", CORRECTED_VERSION, baseline),
                    ("INDICATORS_AND_CONFIRMATION", CORRECTED_VERSION, corrected),
                ):
                    market = frames[version]["windows"][target]
                    context = {
                        "symbol": target,
                        "environment": "SANDBOX",
                        "assembled_at": observed,
                        "market_only": True,
                        "sources": {
                            "s3": market_envelope("s3", "SANDBOX", target, observed, market)
                        },
                        "directional": {
                            "market": market,
                            "price": market["15m"]["close"],
                            "regime": regime,
                            "short_ratio": None,
                        },
                    }
                    plan, _ = market_plan(
                        signal, context,
                        {"source": "s3", "strategy": strategy, "policy": settings},
                    )
                    variants[label] = {
                        "plan": asdict(plan),
                        "feature_version": version,
                        "confirmation_version": settings["entry.confirmation_version"],
                        "event_present_in_version": any(
                            e["type"] == event["type"] for e in events[version]
                        ),
                        "reward_estimate_kind": "REALIZED_MOVE_PROXY_NOT_FORECAST",
                    }
                probes.append({
                    "event_origin": origin,
                    "signal": event["type"],
                    "strength": event["strength"],
                    "strategy": strategy,
                    "variants": variants,
                })
    fields = ("close", "ema20", "ema60", "atr", "atr_pct", "close_pos", "atr_reference_pct")
    return {
        "version": "strategy-correctness-review-v1",
        "symbol": target,
        "closed_at_ms": observed,
        "input_digest": digest(canonical(batch)),
        "regime_scenario": regime,
        "policy_digest": digest(canonical(baseline)),
        "generated_events": {
            version: [{"type": e["type"], "strength": e["strength"]} for e in values]
            for version, values in events.items()
        },
        "features": {
            version: {
                name: {field: market[name].get(field) for field in fields}
                for name in ("15m", "1h", "4h", "24h")
            }
            for version, frame in frames.items()
            for market in (frame["windows"][target],)
        },
        "probes": probes,
        "historical_availability_at_entry": "NOT_VERIFIED",
        "full_account_or_jev_admission": "NOT_RUN",
        "stateful_breakout_history": "NOT_REPLAYED_EMPTY_PRIOR_STATE",
        "alternate_orders_or_pnl": None,
        "profitability_validated": False,
        "execution_authorized": False,
    }
