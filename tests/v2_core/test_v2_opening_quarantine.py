import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict, replace
from types import SimpleNamespace

import pytest
from test_v2_intent_admission import database as database_fixture

from services import v2_opening_quarantine as module
from services.v2_opening_quarantine import (
    OpeningQuarantineContext,
    quarantine_key,
    quarantine_rejection,
)
from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest
from v2_core.state import BusinessState

database = database_fixture
SCOPE = AccountScope("BINANCE", "qa-primary", "SANDBOX", "FUTURES")


def signal(**changes):
    return {"environment": "SANDBOX", "symbol": "BRUSDT", "source": "s3", **changes}


def proof(scope=SCOPE, target="BRUSDT", *, deleted=False):
    key = quarantine_key(scope, target)
    body = {
        "account_scope": asdict(scope),
        "symbol": target,
        "state_id": key.identity,
        "version": 1,
        "deleted": deleted,
        "payload_digest": digest("{}"),
    }
    return {"opening_quarantine": {**body, "digest": digest(canonical(body))}}


@pytest.mark.parametrize("deleted", [False, True])
def test_same_scope_proof_is_terminal_without_order_or_market_claims(deleted):
    context = proof(deleted=deleted)
    result = quarantine_rejection(signal(), context, scope=SCOPE)
    assert result.action == "IGNORED"
    assert result.rationale == "SYMBOL_OPENING_QUARANTINED"
    assert result.side is None and result.quantity is None
    features = json.loads(result.features_json)
    assert features["quarantine"] == context["opening_quarantine"]
    assert features["execution_authorized"] is False
    assert features["admission_authorized"] is False
    assert not {"sources", "assembled_at", "market_plan", "sizing"} & features.keys()


def test_absent_proof_is_not_an_allow_decision():
    assert quarantine_rejection(signal(), {}, scope=SCOPE) is None


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 0},
        {"version": True},
        {"deleted": 1},
        {"digest": "0" * 64},
        {"payload_digest": "not-a-digest"},
        {"state_id": "untrusted-state"},
        {"symbol": "BTCUSDT"},
        {"extra": "unrecognized"},
    ],
)
def test_corrupt_proof_cannot_inject_terminal_decision(changes):
    context = proof()
    context["opening_quarantine"].update(changes)
    with pytest.raises(ValueError, match="PROOF_INVALID"):
        quarantine_rejection(signal(), context, scope=SCOPE)


@pytest.mark.parametrize(
    "other",
    [
        replace(SCOPE, account_id="qa-other"),
        replace(SCOPE, environment="LIVE"),
    ],
)
def test_even_valid_digest_from_other_account_or_environment_is_rejected(other):
    with pytest.raises(ValueError, match="PROOF_INVALID"):
        quarantine_rejection(signal(), proof(other), scope=SCOPE)


def test_valid_digest_for_other_symbol_cannot_be_replayed():
    with pytest.raises(ValueError, match="PROOF_INVALID"):
        quarantine_rejection(signal(), proof(target="BTCUSDT"), scope=SCOPE)


def test_signal_cannot_cross_environment():
    with pytest.raises(ValueError, match="SIGNAL_SCOPE_MISMATCH"):
        quarantine_rejection(signal(environment="LIVE"), proof(), scope=SCOPE)


def test_outer_row_environment_need_not_be_duplicated_in_snapshot(monkeypatch):
    snapshot = signal()
    del snapshot["environment"]
    monkeypatch.setattr(
        module,
        "BusinessState",
        lambda _: SimpleNamespace(
            read=lambda key: SimpleNamespace(
                version=1,
                deleted=False,
                payload_json="{}",
            )
        ),
    )
    context = OpeningQuarantineContext(
        lambda: None,
        lambda _: pytest.fail("quarantine must skip delegate"),
        scope=SCOPE,
    )(snapshot)
    assert quarantine_rejection(snapshot, context, scope=SCOPE).action == "IGNORED"


def test_plain_dict_scope_not_accepted():
    with pytest.raises(TypeError, match="TYPED"):
        quarantine_rejection(signal(), proof(), scope=asdict(SCOPE))
    with pytest.raises(TypeError, match="TYPED"):
        OpeningQuarantineContext(lambda: None, lambda x: x, scope=asdict(SCOPE))


def test_context_hit_never_calls_expensive_delegate(monkeypatch):
    read_keys = []

    class Store:
        def __init__(self, connect):
            pass

        def read(self, key):
            read_keys.append(key)
            return SimpleNamespace(
                version=2, deleted=False, payload_json='{"reason":"incident"}'
            )

    monkeypatch.setattr(module, "BusinessState", Store)
    context = OpeningQuarantineContext(
        lambda: None,
        lambda _: pytest.fail("market/account/model must not run"),
        scope=SCOPE,
    )(signal())
    assert len(read_keys) == 1 and read_keys[0] == quarantine_key(SCOPE, "BRUSDT")
    assert set(context) == {"opening_quarantine"}
    assert context["opening_quarantine"]["payload_digest"] == digest(
        '{"reason":"incident"}'
    )
    assert quarantine_rejection(signal(), context, scope=SCOPE).action == "IGNORED"


def test_absent_row_preserves_delegate_result_and_input_identity(monkeypatch):
    monkeypatch.setattr(
        module, "BusinessState", lambda _: SimpleNamespace(read=lambda key: None)
    )
    raw, original = signal(), {"normal": "context"}

    def delegate(value):
        assert value is raw
        return original

    assert (
        OpeningQuarantineContext(lambda: None, delegate, scope=SCOPE)(raw) is original
    )


def test_database_failure_does_not_call_delegate_or_claim_ignored(monkeypatch):
    def broken(key):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(module, "BusinessState", lambda _: SimpleNamespace(read=broken))
    context = OpeningQuarantineContext(
        lambda: None,
        lambda _: pytest.fail("unverified scope must not proceed"),
        scope=SCOPE,
    )
    with pytest.raises(RuntimeError):
        context(signal())


def test_output_evidence_is_immutable_from_callers():
    context = proof()
    decision = quarantine_rejection(signal(), context, scope=SCOPE)
    original = decision.features_json
    context["opening_quarantine"]["version"] = 999
    assert decision.features_json == original


def test_parallel_pure_validation_has_no_cross_scope_shared_cache():
    good, wrong = proof(), proof(replace(SCOPE, account_id="qa-other"))

    def inspect(index):
        value = deepcopy(good if index % 2 == 0 else wrong)
        if index % 2:
            with pytest.raises(ValueError, match="PROOF_INVALID"):
                quarantine_rejection(signal(), value, scope=SCOPE)
            return "REJECTED_FOREIGN_PROOF"
        return quarantine_rejection(signal(), value, scope=SCOPE).action

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(inspect, range(100)))
    assert results.count("IGNORED") == 50
    assert results.count("REJECTED_FOREIGN_PROOF") == 50


@pytest.mark.parametrize("deleted", [False, True])
def test_pg_existing_quarantine_namespace_is_read_only_and_scoped(database, deleted):
    store = BusinessState(database)
    key = quarantine_key(SCOPE, "BRUSDT")
    store.change(
        key,
        expected_version=0,
        request_key="seed",
        payload={} if deleted else {"blocked": True},
        reason="QA_INCIDENT",
        deleted=deleted,
    )
    seen = []
    wrapper = OpeningQuarantineContext(
        database, lambda item: seen.append(item) or {"normal": True}, scope=SCOPE
    )
    with ThreadPoolExecutor(max_workers=4) as pool:
        contexts = list(pool.map(wrapper, [signal()] * 8))
    assert not seen
    for context in contexts:
        assert quarantine_rejection(signal(), context, scope=SCOPE).action == "IGNORED"
    assert store.read(key).version == 1
    assert wrapper(signal(symbol="BTCUSDT")) == {"normal": True}
    other = OpeningQuarantineContext(
        database, lambda _: {"other": True}, scope=replace(SCOPE, account_id="qa-other")
    )
    assert other(signal()) == {"other": True}
