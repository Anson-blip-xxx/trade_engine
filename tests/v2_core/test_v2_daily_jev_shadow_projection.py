"""Frozen shadow receipts identify mode, never model success or gate approval."""

import json
from copy import deepcopy
from dataclasses import asdict, replace
from uuid import UUID

import pytest

import services.v2_daily_account_review as subject
import tests.v2_core.test_v2_daily_account_review as helpers
from tests.v2_core.test_v2_daily_account_review import database, labels

__all__ = ["database", "labels"]


def projection(signal_id=None, scope=helpers.SCOPE):
    signal_id = signal_id or str(UUID(int=12))
    consumer_scope = [*asdict(scope).values(), "s6"]
    return {
        "shadow_present": True,
        "gate_present": False,
        "signal_id": signal_id,
        "consumer_scope": consumer_scope,
        "receipt": {
            "schema": "jev-shadow-receipt-v1",
            "status": "SUBMITTED_NOT_DURABLE",
            "coverage": "MISSING_UNTIL_DURABLE_RESULT",
            "decision_effect": "NONE",
            "execution_authorized": False,
            "risk_override": False,
            "admission_authorized": False,
            "signal_id": signal_id,
            "consumer": json.dumps(consumer_scope, separators=(",", ":")),
            "candidate_digest": "a" * 64,
        },
    }


def row(value):
    return (str(UUID(int=1)), True, "s3", *((None,) * 10), value)


@pytest.mark.parametrize("status", sorted(subject._JEV_SHADOW_STATUSES))
def test_receipt_status_never_means_model_evaluated(status, labels):
    value = projection()
    value["receipt"].update(
        status=status,
        coverage="MISSING"
        if status != "SUBMITTED_NOT_DURABLE"
        else "MISSING_UNTIL_DURABLE_RESULT",
    )
    result = helpers.load(helpers.Connection(jev=[row(value)]))
    jev = result["jev_participation"]
    assert jev["by_participation"]["SHADOW_ONLY"] == {
        "settled_count": 1,
        "net_usdt": "19.8",
    }
    assert jev["model_evaluated"] == {"settled_count": 0, "net_usdt": "0"}
    assert jev["fallback_causes"] == {}
    assert helpers.SCOPE.account_id not in json.dumps(result)
    assert value["receipt"]["candidate_digest"] not in json.dumps(result)


@pytest.mark.parametrize(
    "key,value",
    [
        ("schema", "future-shadow"),
        ("status", "APPROVED"),
        ("status", []),
        ("decision_effect", "VETO"),
        ("execution_authorized", True),
        ("risk_override", 0),
        ("admission_authorized", None),
        ("consumer", '["wrong-account"]'),
        ("signal_id", str(UUID(int=13))),
        ("candidate_digest", "invalid"),
        ("coverage", "MODEL_COMPLETED"),
    ],
)
def test_invalid_shadow_identity_or_authority_is_unknown(key, value):
    data = projection()
    data["receipt"][key] = value
    assert subject._jev_classification(row(data)) == "UNKNOWN"


@pytest.mark.parametrize(
    "defect",
    [
        "gate",
        "missing_evidence",
        "bad_signal",
        "live_scope",
        "non_object",
        "null_receipt",
    ],
)
def test_conflicting_or_unbound_shadow_is_unknown(defect):
    value = projection()
    if defect == "gate":
        value["gate_present"] = True
    elif defect == "bad_signal":
        value["signal_id"] = "not-uuid"
    elif defect == "live_scope":
        value = projection(scope=replace(helpers.SCOPE, environment="LIVE"))
    elif defect == "null_receipt":
        value["receipt"] = None
    elif defect == "non_object":
        value = []
    record = row(value)
    if defect == "missing_evidence":
        record = (record[0], False, *record[2:])
    assert subject._jev_classification(record) == "UNKNOWN"


def test_legacy_gate_and_new_gate_without_shadow_keep_existing_classification():
    legacy = helpers.jev_row()
    assert subject._jev_classification(legacy) == "MODEL_APPROVED"
    value = projection()
    value["shadow_present"] = False
    value["gate_present"] = True
    assert subject._jev_classification((*legacy, value)) == "MODEL_APPROVED"
    value["shadow_present"] = True
    assert subject._jev_classification((*legacy, value)) == "UNKNOWN"


def test_projection_adds_no_join_or_model_query_and_keeps_verified_episode_bound():
    sql = subject._JEV_SQL
    assert sql.count("JOIN") == 1
    assert "LIMIT 500" in sql and "i.intent_id=ANY(%s::uuid[])" in sql
    assert (
        "jsonb_build_array(i.exchange,i.account_id,i.environment,i.product,i.producer)"
        in sql
    )
    assert "i.signal_id::text" in sql
    assert "e.config," not in sql and "e.snapshot," not in sql


def test_pg_shadow_projection_is_signal_and_account_bound(database, monkeypatch):
    original = helpers.evidence_helpers.add_decision

    def with_shadow(conn, signal, **kwargs):
        value = projection(signal[0], kwargs.get("scope", helpers.SCOPE))
        kwargs["fields"] = {
            "jev_shadow": deepcopy(value["receipt"]),
            "prompt": "NEVER_EXPORT",
        }
        return original(conn, signal, **kwargs)

    monkeypatch.setattr(helpers.evidence_helpers, "add_decision", with_shadow)
    with database() as conn:
        helpers.add_trade(conn, scope=helpers.SCOPE)
        helpers.add_trade(
            conn, scope=replace(helpers.SCOPE, account_id="foreign-account")
        )
    result = helpers.read_pg(database)
    jev = result["jev_participation"]
    assert jev["status"] == "AVAILABLE"
    assert jev["by_participation"]["SHADOW_ONLY"] == {
        "settled_count": 1,
        "net_usdt": "19.8",
    }
    assert jev["model_evaluated"] == {"settled_count": 0, "net_usdt": "0"}
    assert "NEVER_EXPORT" not in json.dumps(result)
