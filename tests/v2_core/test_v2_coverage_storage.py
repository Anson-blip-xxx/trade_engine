import json
from copy import deepcopy
from dataclasses import replace
from uuid import uuid4

import pytest
from test_v2_intent_admission import database as database_fixture

from services.v2_coverage_storage import compact_legacy_batch
from v2_core.account_risk import AccountScope
from v2_core.coverage_evidence import pack_coverage, unpack_coverage
from v2_core.state import BusinessState, StateKey

database = database_fixture


def key():
    return StateKey(
        "BINANCE",
        "storage-test",
        "SANDBOX",
        "FUTURES",
        "account-coverage-evidence-v1",
        str(uuid4()),
    )


def payload():
    return {
        "before": {"orders": ["evidence"] * 1000},
        "after": {"orders": ["evidence"] * 1000},
        "inventory": {},
        "result": {"status": "CLEAR"},
    }


def create(database, target, *, legacy=False):
    store = BusinessState(database)
    assert (
        store.change(
            target,
            expected_version=0,
            request_key="audit",
            payload=payload(),
            reason="ACCOUNT_COVERAGE_AUDIT",
        ).code
        == "APPLIED"
    )
    if legacy:
        with database() as c:
            c.execute("ALTER TABLE v2_business_state DISABLE TRIGGER v2_state_version")
            c.execute(
                "UPDATE v2_business_state SET payload=%s::jsonb WHERE state_id=%s",
                (json.dumps(payload()), target.identity),
            )
            c.execute("ALTER TABLE v2_business_state ENABLE TRIGGER v2_state_version")
    return store


def test_new_coverage_stores_full_payload_once_and_read_is_compatible(database):
    target = key()
    store = create(database, target)
    with database() as c:
        current = c.execute(
            "SELECT payload FROM v2_business_state WHERE state_id=%s",
            (target.identity,),
        ).fetchone()[0]
        history = c.execute(
            "SELECT payload FROM v2_state_history WHERE state_id=%s", (target.identity,)
        ).fetchone()[0]
    assert current == {"_v2_state_history_ref": target.identity, "version": 1}
    assert history == payload()
    assert json.loads(store.read(target).payload_json) == history
    assert (
        store.change(
            target,
            expected_version=0,
            request_key="audit",
            payload=payload(),
            reason="ACCOUNT_COVERAGE_AUDIT",
        ).code
        == "ALREADY_APPLIED"
    )


def test_compaction_preserves_identity_history_and_semantic_payload(database):
    target = key()
    store = create(database, target, legacy=True)
    before = store.read(target)
    with database() as c:
        history = c.execute(
            "SELECT * FROM v2_state_history WHERE state_id=%s", (target.identity,)
        ).fetchall()
        assert compact_legacy_batch(c, [target.identity]) == 1
        assert (
            c.execute(
                "SELECT * FROM v2_state_history WHERE state_id=%s", (target.identity,)
            ).fetchall()
            == history
        )
        assert (
            c.execute(
                "SELECT tgenabled FROM pg_trigger WHERE tgrelid='v2_business_state'::regclass AND tgname='v2_state_version'"
            ).fetchone()[0]
            == "O"
        )
    assert store.read(target) == before


@pytest.mark.parametrize(
    "kind", ["foreign_namespace", "version", "mismatch", "duplicate", "missing"]
)
def test_narrow_compaction_rejects_unproven_or_other_state(database, kind):
    target = key()
    if kind == "foreign_namespace":
        target = replace(target, namespace="testnet-protection-v1")
    store = create(database, target, legacy=True)
    if kind == "version":
        store.change(
            target,
            expected_version=1,
            request_key="second",
            payload=payload(),
            reason="ACCOUNT_COVERAGE_AUDIT",
        )
    if kind == "mismatch":
        with database() as c:
            c.execute("ALTER TABLE v2_business_state DISABLE TRIGGER v2_state_version")
            c.execute(
                "UPDATE v2_business_state SET payload='{}' WHERE state_id=%s",
                (target.identity,),
            )
            c.execute("ALTER TABLE v2_business_state ENABLE TRIGGER v2_state_version")
    ids = (
        [target.identity, target.identity]
        if kind == "duplicate"
        else [str(uuid4())]
        if kind == "missing"
        else [target.identity]
    )
    with pytest.raises(ValueError), database() as c:
        compact_legacy_batch(c, ids)
    with database() as c:
        assert (
            c.execute(
                "SELECT tgenabled FROM pg_trigger WHERE tgrelid='v2_business_state'::regclass AND tgname='v2_state_version'"
            ).fetchone()[0]
            == "O"
        )


def test_compaction_transaction_rollback_restores_representation(database):
    target = key()
    create(database, target, legacy=True)
    with pytest.raises(RuntimeError), database() as c:
        compact_legacy_batch(c, [target.identity])
        raise RuntimeError("injected abort")
    with database() as c:
        assert (
            c.execute(
                "SELECT payload FROM v2_business_state WHERE state_id=%s",
                (target.identity,),
            ).fetchone()[0]
            == payload()
        )


def test_other_namespaces_stay_inline(database):
    target = replace(key(), namespace="runtime-policy-v1")
    create(database, target)
    with database() as c:
        assert (
            c.execute(
                "SELECT payload FROM v2_business_state WHERE state_id=%s",
                (target.identity,),
            ).fetchone()[0]
            == payload()
        )


def test_forged_reference_fails_closed(database):
    target = key()
    store = create(database, target)
    with database() as c:
        c.execute("ALTER TABLE v2_business_state DISABLE TRIGGER v2_state_version")
        c.execute(
            "UPDATE v2_business_state SET payload=jsonb_set(payload,'{_v2_state_history_ref}',to_jsonb(%s::text)) WHERE state_id=%s",
            (str(uuid4()), target.identity),
        )
        c.execute("ALTER TABLE v2_business_state ENABLE TRIGGER v2_state_version")
    with pytest.raises(ValueError, match="invalid state history reference"):
        store.read(target)


def facts_payload(count=130):
    facts = {
        "episodes": [],
        "orders": [
            {"order_id": str(i), "version": 1, "status": "FILLED"} for i in range(count)
        ],
        "protections": [],
    }
    return {
        "before": facts,
        "after": deepcopy(facts),
        "inventory": {"observation_id": "test"},
        "result": {"status": "ACCOUNT_COVERAGE_CLEAR"},
    }


def test_dedup_chunks_preserve_full_content_and_only_changed_record_grows(database):
    scope = AccountScope("BINANCE", "storage-test", "SANDBOX", "FUTURES")
    original = facts_payload(1500)
    with database() as c:
        packed = pack_coverage(c, scope, original)
        count = c.execute("SELECT count(*) FROM v2_coverage_blobs").fetchone()[0]
        assert count == 1500 + 24 + 1
        assert packed["before_ref"] == packed["after_ref"]
        assert unpack_coverage(c, scope, packed) == original
        assert pack_coverage(c, scope, original) == packed
        assert (
            c.execute("SELECT count(*) FROM v2_coverage_blobs").fetchone()[0] == count
        )
        changed = deepcopy(original)
        changed["after"]["orders"][900]["version"] = 2
        changed_packed = pack_coverage(c, scope, changed)
        assert (
            c.execute("SELECT count(*) FROM v2_coverage_blobs").fetchone()[0]
            == count + 3
        )
        assert unpack_coverage(c, scope, changed_packed) == changed
        assert len(json.dumps(packed)) < len(json.dumps(original)) / 100


def test_content_addressing_is_scoped_and_order_preserving(database):
    scope = AccountScope("BINANCE", "storage-test", "SANDBOX", "FUTURES")
    original = facts_payload()
    with database() as c:
        packed = pack_coverage(c, scope, original)
        with pytest.raises(ValueError, match="SCOPE"):
            unpack_coverage(c, replace(scope, account_id="other"), packed)
        second = pack_coverage(c, replace(scope, account_id="other"), original)
        assert packed["before_ref"] != second["before_ref"]
        original["after"]["orders"].reverse()
        reordered = pack_coverage(c, scope, original)
        assert unpack_coverage(c, scope, reordered) == original


def test_packed_state_read_is_same_as_legacy_and_atomic_rollback(database):
    target = key()
    original = facts_payload()
    with database() as c:
        encoded = pack_coverage(c, target, original)
    store = BusinessState(database)
    store.change(
        target,
        expected_version=0,
        request_key="packed",
        payload=encoded,
        reason="ACCOUNT_COVERAGE_AUDIT",
    )
    assert json.loads(store.read(target).payload_json) == original
    with database() as c:
        before = c.execute("SELECT count(*) FROM v2_coverage_blobs").fetchone()[0]
    with pytest.raises(RuntimeError), database() as c:
        pack_coverage(c, replace(target, account_id="rolled-back"), original)
        raise RuntimeError("injected failure")
    with database() as c:
        assert (
            c.execute("SELECT count(*) FROM v2_coverage_blobs").fetchone()[0] == before
        )


def test_missing_blob_and_digest_mutation_fail_closed(database):
    scope = AccountScope("BINANCE", "storage-test", "SANDBOX", "FUTURES")
    with database() as c:
        encoded = pack_coverage(c, scope, facts_payload())
        encoded["after_ref"] = "0" * 64
        with pytest.raises(ValueError, match="MISSING"):
            unpack_coverage(c, scope, encoded)
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation), database() as c:
        c.execute(
            "INSERT INTO v2_coverage_blobs(blob_id,content_json) VALUES (%s,%s)",
            ("f" * 64, "{}"),
        )
    with pytest.raises(psycopg.errors.RaiseException), database() as c:
        c.execute("DELETE FROM v2_coverage_blobs")
