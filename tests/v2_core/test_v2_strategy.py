import pytest

from v2_core.strategy import StrategyDecision


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "BUY", "rationale": "bad"},
        {"action": "OPEN", "rationale": "why", "side": "LONG", "quantity": "1"},
        {"action": "OPEN", "rationale": "why", "side": "BUY", "quantity": 0.1},
        {"action": "OPEN", "rationale": "why", "side": "BUY", "quantity": "NaN"},
        {"action": "IGNORED", "rationale": "why", "quantity": "1"},
        {"action": "IGNORED", "rationale": ""},
        {"action": "IGNORED", "rationale": "why", "features_json": '{"price":0.1}'},
        {
            "action": "IGNORED",
            "rationale": "why",
            "features_json": '{"api_key":"private"}',
        },
    ],
)
def test_strategy_output_rejects_ambiguous_or_secret_bearing_decisions(kwargs):
    with pytest.raises(ValueError):
        StrategyDecision(**kwargs)
