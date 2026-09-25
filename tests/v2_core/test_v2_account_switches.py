from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from test_v2_capital_journal import setup as journal_setup
from test_v2_directional_replay import SCOPE
from test_v2_intent_admission import database as database_fixture

from v2_core.account_draining import AccountDraining
from v2_core.account_switches import AccountSwitches

database = database_fixture
setup = journal_setup


@pytest.fixture
def switch_setup(setup):
    j, registry, tenant, other, a, b, foreign, live = setup
    with j.connect() as c:
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260925_account_switches.sql"
            ).read_text()
        )
    return AccountSwitches(j.connect), registry, tenant, other, a, b, foreign, live


def request(s, t, a, b, request_id=None):
    return s.request(t, a, b, request_id=request_id or uuid4())


def counts(s):
    with s.connect() as c:
        return tuple(
            c.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "v2_account_switches",
                "v2_account_switch_claims",
                "v2_account_switch_events",
            )
        )


def test_request_replay_claims_and_cancel(switch_setup):
    s, _, t, _, a, b, *_ = switch_setup
    rid = uuid4()
    first = request(s, t, a, b, rid)
    assert request(s, t, a, b, rid) == first
    assert counts(s) == (1, 2, 1)
    assert s.inspect(t, first["switch_id"])["source_drain"]["status"] == "NOT_DRAINING"
    with pytest.raises(ValueError, match="ALREADY_IN_SWITCH"):
        request(s, t, b, a)
    with pytest.raises(ValueError, match="REQUEST_CONFLICT"):
        request(s, t, b, a, rid)
    cancelled = s.advance(t, first["switch_id"], expected_version=1, action="CANCEL")
    assert cancelled["status"] == "CANCELLED"
    assert (
        s.advance(t, first["switch_id"], expected_version=1, action="CANCEL")
        == cancelled
    )
    assert request(s, t, a, b, rid) == cancelled
    assert counts(s) == (1, 0, 2)
    assert request(s, t, b, a)["status"] == "REQUESTED"


@pytest.mark.parametrize(
    "endpoint,error",
    [("self", "DISTINCT"), ("foreign", "NOT_FOUND"), ("live", "SANDBOX")],
)
def test_scope_rejections(switch_setup, endpoint, error):
    s, _, t, _, a, _, foreign, live = switch_setup
    with pytest.raises(ValueError, match=error):
        request(s, t, a, {"self": a, "foreign": foreign, "live": live}[endpoint])
    assert counts(s) == (0, 0, 0)


def test_drain_retains_claims_and_does_not_activate_target(switch_setup):
    s, _, t, other, a, b, *_ = switch_setup
    sid = request(s, t, a, b)["switch_id"]
    result = s.advance(t, sid, expected_version=1, action="DRAIN_SOURCE")
    assert s.advance(t, sid, expected_version=1, action="DRAIN_SOURCE") == result
    assert result["status"] == "DRAINING"
    assert not result["switch_complete"] and not result["target_activation_authorized"]
    assert counts(s) == (1, 2, 2)
    assert s.inspect(t, sid)["source_drain"]["status"] == "DRAINING"
    assert AccountDraining(s.connect).inspect(t, b)["status"] == "NOT_DRAINING"
    for action, version, owner, error in [
        ("CANCEL", 2, t, "CONFLICT"),
        ("ACTIVATE", 2, t, "EXPLICIT"),
        ("CANCEL", True, t, "EXPLICIT"),
        ("CANCEL", 1, other, "NOT_FOUND"),
    ]:
        with pytest.raises(ValueError, match=error):
            s.advance(owner, sid, expected_version=version, action=action)
    with pytest.raises(ValueError, match="NOT_FOUND"):
        s.inspect(other, sid)


@pytest.mark.parametrize("same_request", [True, False])
def test_concurrent_requests(switch_setup, same_request):
    s, _, t, _, a, b, *_ = switch_setup
    rid = uuid4()

    def run(_):
        try:
            return request(s, t, a, b, rid if same_request else uuid4())["switch_id"]
        except ValueError as exc:
            assert str(exc) == "ACCOUNT_ALREADY_IN_SWITCH"
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(8)))
    assert len(set(filter(None, results))) == 1
    assert sum(x is not None for x in results) == (8 if same_request else 1)
    assert counts(s) == (1, 2, 1)


def test_cancel_drain_race(switch_setup):
    s, _, t, _, a, b, *_ = switch_setup
    sid = request(s, t, a, b)["switch_id"]

    def run(action):
        try:
            return s.advance(t, sid, expected_version=1, action=action)["status"]
        except ValueError as exc:
            assert str(exc) == "SWITCH_VERSION_OR_STATE_CONFLICT"
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["CANCEL", "DRAIN_SOURCE"]))
    (winner,) = [r for r in results if r]
    assert counts(s) == (1, 2 if winner == "DRAINING" else 0, 2)
    assert s.inspect(t, sid)["source_drain"]["status"] == (
        "DRAINING" if winner == "DRAINING" else "NOT_DRAINING"
    )


@pytest.mark.parametrize("rotate_target", [False, True])
def test_rotation_invalidates_pinned_binding(switch_setup, rotate_target):
    s, registry, t, _, a, b, *_ = switch_setup
    sid = request(s, t, a, b)["switch_id"]
    registry.rotate_credential(
        t,
        b if rotate_target else a,
        credential_ref=uuid4(),
        expected_version=1,
        request_id=uuid4(),
    )
    with pytest.raises(ValueError, match="BINDING_CHANGED"):
        s.advance(t, sid, expected_version=1, action="DRAIN_SOURCE")
    assert "ACCOUNT_BINDING_CHANGED" in s.inspect(t, sid)["activation_blockers"]
    assert AccountDraining(s.connect).inspect(t, a)["status"] == "NOT_DRAINING"
    s.advance(t, sid, expected_version=1, action="CANCEL")
    new = request(s, t, a, b)
    assert new["target_version" if rotate_target else "source_version"] == 2


def test_audit_failure_rolls_back_drain(switch_setup):
    s, _, t, _, a, b, *_ = switch_setup
    sid = request(s, t, a, b)["switch_id"]
    with s.connect() as c:
        c.execute("""CREATE FUNCTION qa_fail_switch() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'qa audit failure'; END $$;
        CREATE TRIGGER qa_fail BEFORE INSERT ON v2_account_switch_events
        FOR EACH ROW EXECUTE FUNCTION qa_fail_switch();""")
    with pytest.raises(psycopg.Error, match="qa audit failure"):
        s.advance(t, sid, expected_version=1, action="DRAIN_SOURCE")
    snap = s.inspect(t, sid)
    assert snap["status"] == "REQUESTED"
    assert snap["source_drain"]["status"] == "NOT_DRAINING"
    assert counts(s) == (1, 2, 1)
    with s.connect() as c:
        c.execute("DROP TRIGGER qa_fail ON v2_account_switch_events")
    assert (
        s.advance(t, sid, expected_version=1, action="DRAIN_SOURCE")["status"]
        == "DRAINING"
    )


def test_either_endpoint_prevents_overlapping_switch(switch_setup):
    s, registry, t, _, a, b, *_ = switch_setup
    c = registry.enroll(
        t,
        replace(SCOPE, account_id="third"),
        display_name="Third",
        credential_ref=uuid4(),
        request_id=uuid4(),
    )["registry_id"]
    request(s, t, a, b)
    for source, target in [(a, c), (c, a), (b, c), (c, b)]:
        with pytest.raises(ValueError, match="ALREADY_IN_SWITCH"):
            request(s, t, source, target)
    assert counts(s) == (1, 2, 1)


def test_rotation_after_drain_never_reopens_source(switch_setup):
    s, registry, t, _, a, b, *_ = switch_setup
    sid = request(s, t, a, b)["switch_id"]
    original = s.advance(t, sid, expected_version=1, action="DRAIN_SOURCE")
    registry.rotate_credential(
        t, a, credential_ref=uuid4(), expected_version=1, request_id=uuid4()
    )
    assert s.advance(t, sid, expected_version=1, action="DRAIN_SOURCE") == original
    snap = s.inspect(t, sid)
    assert "ACCOUNT_BINDING_CHANGED" in snap["activation_blockers"]
    assert snap["source_drain"]["status"] == "DRAINING"
    with pytest.raises(ValueError, match="CONFLICT"):
        s.advance(t, sid, expected_version=2, action="CANCEL")
    assert counts(s) == (1, 2, 2)


@pytest.mark.parametrize(
    "statement",
    [
        "DELETE FROM v2_account_switches",
        "UPDATE v2_account_switches SET source_version=2,version=2,status='DRAINING'",
        "UPDATE v2_account_switches SET status='DRAINING'",
        "DELETE FROM v2_account_switch_events",
        "UPDATE v2_account_switch_events SET evidence='{}'",
    ],
)
def test_sql_history_guards(switch_setup, statement):
    s, _, t, _, a, b, *_ = switch_setup
    request(s, t, a, b)
    with pytest.raises(psycopg.Error), s.connect() as c:
        c.execute(statement)
    assert counts(s) == (1, 2, 1)
