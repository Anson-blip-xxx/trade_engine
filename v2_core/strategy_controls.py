"""Owner-console editable subset. No execution toggles or credential settings.

These are operational validation boundaries, not proven optimal parameters.
All other runtime settings remain readable but cannot be changed by this UI.
"""

import re
from decimal import Decimal

from v2_core.runtime_policy import SCHEMA, resolve

FIELDS = {
    "capital.pool_fraction": "账户总保证金预算比例",
    "sizing.pool_fraction": "定仓资金池比例（须与总预算一致）",
    "capital.margin_buffer": "保证金预占缓冲倍数",
    "sizing.max_margin_fraction": "单笔保证金上限比例",
    "sizing.risk_fraction": "单笔计划止损风险比例",
    "portfolio_risk.max_fraction": "组合计划止损风险比例",
    "sizing.min_allocation": "剩余资金池最低分配比例",
    "sizing.max_allocation": "剩余资金池最高分配比例",
    "sizing.cost_buffer_fraction": "定仓成本缓冲比例",
}


def catalog():
    return [
        {
            "key": key,
            "label": label,
            "type": "decimal",
            "minimum": SCHEMA[key][1],
            "maximum": "0.70"
            if key in {"capital.pool_fraction", "sizing.pool_fraction"}
            else SCHEMA[key][2],
            "effect": "仅后续定仓/资金准入；不重写旧决策、不强制平仓",
        }
        for key, label in FIELDS.items()
    ]


def validate_changes(before, changes):
    if not isinstance(changes, dict) or not changes or set(changes) - FIELDS.keys():
        raise ValueError("POLICY_FIELD_NOT_EDITABLE")
    if any(
        type(v) is not str
        or len(v) > 80
        or re.fullmatch(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)", v) is None
        for v in changes.values()
    ):
        raise ValueError("POLICY_VALUE_INVALID")
    values = resolve({**before, **changes})
    if not values["capital.enabled"] or not values["portfolio_risk.enabled"]:
        raise ValueError("POLICY_REQUIRES_PROVISIONED_CAPITAL_RISK")
    if Decimal(values["capital.pool_fraction"]) > Decimal("0.70"):
        raise ValueError("POLICY_RESERVE_30_PERCENT_REQUIRED")
    if Decimal(values["sizing.max_margin_fraction"]) > Decimal(
        values["capital.pool_fraction"]
    ):
        raise ValueError("POLICY_SINGLE_MARGIN_EXCEEDS_POOL")
    if Decimal(values["sizing.risk_fraction"]) > Decimal(
        values["portfolio_risk.max_fraction"]
    ):
        raise ValueError("POLICY_SINGLE_RISK_EXCEEDS_PORTFOLIO")
    if all(Decimal(values[k]) == Decimal(before[k]) for k in changes):
        raise ValueError("POLICY_NO_CHANGE")
    return values


def budget_examples(settings, *, base="1000"):
    """Illustration, not account capacity or backtest. No venue calls."""
    from dataclasses import asdict

    from v2_core.directional import DirectionalPlan, size_candidate

    rows = []
    for stop in ("0.01", "0.04", "0.08"):
        plan = DirectionalPlan(
            "CANDIDATE",
            "S6",
            "TREND_UP",
            "LONG",
            100,
            "ILLUSTRATION",
            "S6A",
            3,
            "ISOLATED",
            stop,
        )
        explanation = {}
        result = size_candidate(
            plan,
            price="100",
            balance=base,
            available_margin=base,
            used_pool_margin="0",
            atr_pct="1",
            quantity_step="0.001",
            price_tick="0.01",
            min_quantity="0.001",
            max_quantity="1000000",
            min_notional="5",
            max_notional="100000000",
            drawdown_factor="1",
            analysis_factor="1",
            policy=settings,
            explanation=explanation,
        )
        rows.append(
            {
                "stop_fraction": stop,
                "result": asdict(result),
                "explanation": explanation,
            }
        )
    return {
        "kind": "ILLUSTRATION_NOT_BACKTEST",
        "base": base,
        "leverage": 3,
        "score": 100,
        "existing_margin": "0",
        "rows": rows,
        "execution_authorized": False,
    }
