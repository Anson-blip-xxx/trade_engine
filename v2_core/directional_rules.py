"""Parameter-only directional rule plugin; no persistence or execution ports."""

from v2_core.runtime_policy import resolve


class DirectionalRules:
    def __init__(self, policy=None):
        self.values = resolve(policy)

    def f(self, name):
        return float(self.values[name])

    def entry_mode(self, price, ema, rsi, flow, side):
        if side == "LONG":
            if (
                price < ema
                and rsi <= self.f("entry.reversal_rsi_long")
                and (flow is None or flow >= self.f("entry.flow_long"))
            ):
                return "LEFT_REVERSAL"
            if price >= ema:
                return "RIGHT_MOMENTUM"
        else:
            if (
                price > ema
                and rsi >= self.f("entry.reversal_rsi_short")
                and (flow is None or flow <= self.f("entry.flow_short"))
            ):
                return "LEFT_REVERSAL"
            if price <= ema:
                return "RIGHT_MOMENTUM"
        return "UNCONFIRMED"

    def takeover_ready(self, price, windows):
        w15, w4, w24 = windows["15m"], windows["4h"], windows["24h"]
        if price <= w24["ema20"] or w24["ema20"] <= w24["ema60"] or w4["chg"] <= 0:
            return False
        if w15["taker_buy_ratio"] is not None and w15["taker_buy_ratio"] < self.f(
            "entry.flow_long"
        ):
            return False
        return abs(price - w15["ema20"]) <= max(
            w15["atr"] * self.f("entry.takeover_pullback_atr"),
            price * self.f("entry.takeover_pullback_fraction"),
        )

    def score(self, strength, atr, extension, flow, age_seconds, side, ratio):
        score = float(strength)
        if flow is not None:
            aligned = (
                flow >= self.f("entry.flow_long")
                if side == "LONG"
                else flow <= self.f("entry.flow_short")
            )
            score += self.f("score.flow_bonus") * (1 if aligned else -1)
        if ratio is not None:
            high, low = self.f("score.ratio_high"), self.f("score.ratio_low")
            bonus, penalty = (
                self.f("score.crowding_bonus"),
                self.f("score.crowding_penalty"),
            )
            score += (
                (bonus if ratio >= high else -penalty if ratio <= low else 0)
                if side == "LONG"
                else (penalty if ratio <= low else -bonus if ratio >= high else 0)
            )
        if atr > self.f("score.atr_threshold"):
            score -= min(
                self.f("score.max_atr_penalty"),
                (atr - self.f("score.atr_threshold")) * self.f("score.atr_penalty"),
            )
        if extension > 0:
            score -= min(
                self.f("score.max_extension_penalty"),
                extension * self.f("score.extension_penalty"),
            )
        if age_seconds > 0:
            score -= min(
                self.f("score.max_age_penalty"),
                age_seconds / self.f("score.age_step_seconds"),
            )
        return max(0, min(100, round(score)))

    def leverage(self, kind, score, atr):
        base = self.values[
            "leverage.pulse"
            if kind.startswith("PULSE_") or kind == "PANIC_SELL"
            else "leverage.pump"
            if kind.startswith("PUMP_")
            else "leverage.trend"
        ]
        if atr >= self.f("leverage.atr_threshold"):
            base = min(base, self.values["leverage.volatile"])
        if score < self.values["leverage.low_score"]:
            return min(base, self.values["leverage.low"])
        if score < self.values["leverage.high_score"] or atr >= self.f(
            "leverage.atr_threshold"
        ):
            return min(base, self.values["leverage.medium"])
        return base
