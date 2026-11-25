"""Historical research contracts are explicit; raw evidence remains immutable."""

from copy import deepcopy
from itertools import combinations

import pytest

import services.v2_mover_gate_shadow as mover
from services.v2_setup_shadow import evaluate_setup_shadow
from tests.v2_core.test_v2_mover_gate_shadow import frozen_sample, metric_sample
from v2_core.evidence import canonical, digest

CASH = {
    "settlement.cash_basis_version": 1,
    "settlement.max_difference_per_fill_usdt": "0.000001",
    "settlement.max_difference_episode_usdt": "0.0001",
}
QUALITY = {
    "trend.breakout_body_guard_enabled": False,
    "trend.entry_quality_shadow_enabled": False,
}
SOURCE_DIGESTS = {
    193: "63e742ad4be439950afa8fc84faf5ca735cc66672cd4879e668c93dfbb2b8ef0",
    195: "c973c0abf5c486112b6a3d83002903259e9d9eba5c60ae9ad3448cd38266ed75",
    198: "1c2dd3ff0dd19ad1b7f59b1e0a1088c6a652bad22baa4d20b152dc05971dffcb",
}


def sample_for_schema(size):
    sample = frozen_sample()
    policy = sample["config"]["policy"]
    policy.update(CASH)
    policy["settlement.cash_basis_version"] = 2
    if size < 198:
        for key in CASH:
            policy.pop(key)
    if size == 193:
        for key in QUALITY:
            policy.pop(key)
    assert len(policy) == size
    sample["config_digest"] = digest(canonical(sample["config"]))
    return sample


def both_research_entries(sample):
    mover_row = mover.evaluate_mover_gate_shadow([sample], environment="SANDBOX")[
        "samples"
    ][0]
    setup = evaluate_setup_shadow(
        sample["signal"],
        sample["context"],
        sample["config"],
        environment="SANDBOX",
        evidence_ref=sample["evidence_ref"],
        config_digest=sample["config_digest"],
    )
    return mover_row, setup


@pytest.mark.parametrize("size", [193, 195, 198])
def test_both_research_entries_preserve_complete_historical_contract(size, monkeypatch):
    sample = sample_for_schema(size)
    original = canonical(sample)
    calls = []
    evaluator = mover.evaluate_trend_market

    def observed(signal, context, config):
        calls.append(deepcopy(config))
        return evaluator(signal, context, config)

    monkeypatch.setattr(mover, "evaluate_trend_market", observed)
    row, setup = both_research_entries(sample)
    assert row["mode"] == "FULL_FROZEN_REPLAY"
    assert setup["status"] == "READY"
    assert row["volume"]["baseline_market_reason"] == "TREND_VOLUME_UNCONFIRMED"
    assert setup["baseline_market_reason"] == "TREND_VOLUME_UNCONFIRMED"
    assert row["volume"]["candidate_market_reason"] == "CANDIDATE"
    assert setup["global_volume075_market_reason"] == "CANDIDATE"
    assert canonical(sample) == original
    assert row["input_digest"] == digest(original)
    compatibility = row["policy_compatibility"]
    assert setup["policy_compatibility"] == compatibility
    assert compatibility["source_config_digest"] == sample["config_digest"]
    assert compatibility["source_schema_digest"] == SOURCE_DIGESTS[size]
    assert compatibility["evaluator_schema_digest"] == SOURCE_DIGESTS[198]
    assert compatibility["historical_code_identity"] == "NOT_PROVEN"
    assert compatibility["original_evidence_modified"] is False
    assert compatibility["adaptation_scope"] == (
        "PURE_MARKET_GATES_ONLY_NOT_ACCOUNTING_REPLAY"
    )
    expected_additions = {
        **(QUALITY if size == 193 else {}),
        **(CASH if size < 198 else {}),
    }
    assert compatibility["explicit_legacy_values"] == expected_additions
    assert compatibility["cash_basis_adaptation"] == (
        "EXPLICIT_LEGACY_CASH_BASIS_V1" if size < 198 else None
    )
    assert len(calls) == 4
    for config in calls:
        assert len(config["policy"]) == 198
        assert config["policy"]["settlement.cash_basis_version"] == (
            1 if size < 198 else 2
        )
    assert (compatibility["evaluation_config_digest"] == sample["config_digest"]) is (
        size == 198
    )


@pytest.mark.parametrize("size", [193, 195])
def test_legacy_values_are_explicit_not_changed_current_defaults(size, monkeypatch):
    sample = sample_for_schema(size)
    for key, default in {**CASH, **QUALITY}.items():
        _, lower, upper = mover.SCHEMA[key]
        changed = (
            True if type(default) is bool else 2 if type(default) is int else "0.009"
        )
        monkeypatch.setitem(mover.SCHEMA, key, (changed, lower, upper))
    adapted, compatibility = mover._replay_config(sample["config"])
    for key, expected in CASH.items():
        assert adapted["policy"][key] == expected
    if size == 193:
        assert all(adapted["policy"][key] is False for key in QUALITY)
    assert compatibility["source_config_digest"] == sample["config_digest"]
    assert both_research_entries(sample)[0]["mode"] == "FULL_FROZEN_REPLAY"


PARTIAL_CASH = [keys for size in (1, 2) for keys in combinations(CASH, size)]


@pytest.mark.parametrize("present", PARTIAL_CASH)
def test_partial_cash_contract_is_unknown_and_never_replayed(present, monkeypatch):
    sample = sample_for_schema(195)
    sample["config"]["policy"].update({key: CASH[key] for key in present})
    sample["config_digest"] = digest(canonical(sample["config"]))
    monkeypatch.setattr(
        mover, "evaluate_trend_market", lambda *args: pytest.fail("no replay")
    )
    row, setup = both_research_entries(sample)
    assert row["mode"] == "UNKNOWN"
    assert row["volume"]["reason"] == "INCOMPLETE_FROZEN_POLICY"
    assert setup["status"] == "UNKNOWN"
    assert setup["reason"] == "INCOMPLETE_FROZEN_POLICY"


@pytest.mark.parametrize("present", PARTIAL_CASH)
def test_metric_only_does_not_hide_partial_cash_contract(present):
    sample = metric_sample()
    sample["config"]["policy"].update({key: CASH[key] for key in present})
    row = mover.evaluate_mover_gate_shadow([sample], environment="SANDBOX")["samples"][
        0
    ]
    assert row["mode"] == "UNKNOWN"
    assert row["volume"]["reason"] == "INCOMPLETE_FROZEN_POLICY"


BAD_CASH = [
    ("settlement.cash_basis_version", value) for value in (True, "2", 2.0, 0, 3, None)
] + [
    (key, value)
    for key in (
        "settlement.max_difference_per_fill_usdt",
        "settlement.max_difference_episode_usdt",
    )
    for value in (
        False,
        0,
        0.001,
        None,
        "-0.0001",
        "0.010000000000001",
        "NaN",
        "Infinity",
        "",
        "1" * 81,
    )
]


@pytest.mark.parametrize("key,value", BAD_CASH)
def test_bad_cash_types_and_ranges_never_reach_either_evaluator(
    key, value, monkeypatch
):
    sample = sample_for_schema(198)
    sample["config"]["policy"][key] = value
    # Floats are rejected by canonical before digest checking in both entries.
    sample["config_digest"] = (
        "f" * 64 if isinstance(value, float) else digest(canonical(sample["config"]))
    )
    monkeypatch.setattr(
        mover, "evaluate_trend_market", lambda *args: pytest.fail("no replay")
    )
    row, setup = both_research_entries(sample)
    assert row["mode"] == "UNKNOWN"
    assert row["error"] is True
    assert setup["status"] == "UNKNOWN"
    assert setup["reason"] == "INVALID_FROZEN_INPUT"


@pytest.mark.parametrize("basis", [1, 2])
@pytest.mark.parametrize("limit", ["0", "0.01"])
def test_complete_cash_contract_accepts_closed_validation_bounds(basis, limit):
    sample = sample_for_schema(198)
    sample["config"]["policy"].update(
        {key: basis if type(default) is int else limit for key, default in CASH.items()}
    )
    sample["config_digest"] = digest(canonical(sample["config"]))
    row, setup = both_research_entries(sample)
    assert row["mode"] == "FULL_FROZEN_REPLAY"
    assert setup["status"] == "READY"


@pytest.mark.parametrize("removed", [(next(iter(QUALITY)),), tuple(QUALITY)])
def test_cash_schema_with_missing_entry_quality_fields_is_not_registered(removed):
    sample = sample_for_schema(198)
    for key in removed:
        sample["config"]["policy"].pop(key)
    sample["config_digest"] = digest(canonical(sample["config"]))
    row, setup = both_research_entries(sample)
    assert row["mode"] == "UNKNOWN"
    assert row["volume"]["reason"] == "INCOMPLETE_FROZEN_POLICY"
    assert setup["status"] == "UNKNOWN"


@pytest.mark.parametrize("size", [193, 195, 198])
@pytest.mark.parametrize("defect", ["unknown", "missing", "type", "wrong_digest"])
def test_non_cash_invalid_evidence_is_not_relaxed(size, defect, monkeypatch):
    sample = sample_for_schema(size)
    policy = sample["config"]["policy"]
    if defect == "unknown":
        policy["settlement.unrecognized"] = "0"
    elif defect == "missing":
        policy.pop("trend.max_atr_pct")
    elif defect == "type":
        policy["entry.min_score"] = "30"
    sample["config_digest"] = (
        "f" * 64 if defect == "wrong_digest" else digest(canonical(sample["config"]))
    )
    monkeypatch.setattr(
        mover, "evaluate_trend_market", lambda *args: pytest.fail("no replay")
    )
    row, setup = both_research_entries(sample)
    assert row["mode"] == "UNKNOWN"
    assert setup["status"] == "UNKNOWN"


@pytest.mark.parametrize("target", ["old195", "future199"])
def test_unregistered_evaluator_package_fails_closed(target, monkeypatch):
    sample = sample_for_schema(195)
    if target == "old195":
        for key in CASH:
            monkeypatch.delitem(mover.SCHEMA, key)
    else:
        monkeypatch.setitem(mover.SCHEMA, "future.risk.setting", (False, None, None))
    monkeypatch.setattr(
        mover, "evaluate_trend_market", lambda *args: pytest.fail("no replay")
    )
    row, setup = both_research_entries(sample)
    assert row["mode"] == "UNKNOWN"
    assert row["volume"]["reason"] == "INCOMPLETE_FROZEN_POLICY"
    assert setup["status"] == "UNKNOWN"
