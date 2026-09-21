from dataclasses import asdict, replace
from pathlib import Path

import pytest
from test_v2_account_inventory import TelegramConnection, sink
from test_v2_protection_child import SCOPE
from test_v2_protection_child import database as database_fixture
from test_v2_protection_child import setup as setup_fixture

from v2_core.protection_supervisor import ProtectionAlertProjector, ProtectionSupervisor
from v2_core.state import StateKey

database = database_fixture
setup = setup_fixture


def test_registered_trigger_discovered_and_replayed_without_duplicate(database, setup):
    data, spec, venue, child = setup
    worker = ProtectionSupervisor(database, venue, scope=SCOPE)
    state_id = child.protection.key(spec).identity
    assert worker.run_once()["results"][state_id]["status"] == "FILLED"
    version = child.protection.read(spec)[0].version
    assert worker.run_once()["results"][state_id]["status"] == "FILLED"
    assert child.protection.read(spec)[0].version == version
    trace = data.trace(spec.episode)
    assert len(trace["orders"]) == 2 and len(trace["fills"]) == 2
    with database() as conn:
        assert (
            conn.execute("SELECT count(*) FROM v2_operational_outbox").fetchone()[0]
            == 0
        )


def test_query_only_and_live_scope_rejected(database, setup):
    _, _, venue, _ = setup
    worker = ProtectionSupervisor(database, venue, scope=SCOPE)
    with pytest.raises(ValueError, match="QUERY_ONLY"):
        worker.child.protection.request("POST", "/fapi/v1/algoOrder", {})
    assert not venue.calls
    with pytest.raises(ValueError, match="scope mismatch"):
        ProtectionSupervisor(database, venue, scope=replace(SCOPE, environment="LIVE"))


def test_account_lock_excludes_parallel_scans(database, setup):
    _, _, venue, _ = setup
    worker = ProtectionSupervisor(database, venue, scope=SCOPE)
    with database() as conn:
        conn.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s,0))",
            ("testnet-protocol-account:" + SCOPE.key,),
        )
        conn.commit()
        assert worker.run_once() == {"status": "BUSY", "results": {}}
    assert not venue.calls
    assert worker.run_once()["status"] == "SCANNED"


@pytest.mark.parametrize("status", ["CANCELED", "EXPIRED", "REJECTED"])
def test_missing_active_protection_alert_is_durable_deduplicated_and_scoped(
    database, setup, status
):
    _, spec, venue, child = setup
    venue.parent["algoStatus"] = status
    worker = ProtectionSupervisor(database, venue, scope=SCOPE)
    state_id = child.protection.key(spec).identity
    assert worker.run_once()["results"][state_id]["status"] == "UNPROTECTED"
    worker.run_once()
    with database() as conn:
        assert (
            conn.execute(
                "SELECT count(*) FROM v2_operational_outbox WHERE event_type='PROTECTION_RECOVERY'"
            ).fetchone()[0]
            == 1
        )
    calls = []
    sink = lambda *args: calls.append(args) or True
    other = ProtectionAlertProjector(
        database, scope=replace(SCOPE, account_id="other"), sink=sink
    )
    assert other.run_scheduled_batch()["claimed"] == 0
    correct = ProtectionAlertProjector(database, scope=SCOPE, sink=sink)
    assert correct.run_scheduled_batch()["delivered"] == 1
    assert correct.run_scheduled_batch()["claimed"] == 0
    assert len(calls) == 1


def test_cursor_survives_restart_and_poison_does_not_starve_other_leg(
    database, setup, monkeypatch
):
    _, spec, venue, child = setup
    other = replace(spec, kind="TAKE_PROFIT_MARKET", trigger_price="101")
    child.protection.save(other, None, {"spec": asdict(other), "status": "SENDING"})
    ids = sorted([child.protection.key(s).identity for s in (spec, other)])

    def fake(payload):
        if child.protection.key(type(spec)(**payload["spec"])).identity == ids[0]:
            raise ValueError("secret must not be stored")
        return {"status": "FILLED"}

    monkeypatch.setattr(
        ProtectionSupervisor, "_result", lambda self, payload: fake(payload)
    )
    a = ProtectionSupervisor(database, venue, scope=SCOPE).run_once(1)
    b = ProtectionSupervisor(database, venue, scope=SCOPE).run_once(1)
    c = ProtectionSupervisor(database, venue, scope=SCOPE).run_once(1)
    assert a["results"] == {
        ids[0]: {"status": "RECOVERY_PENDING", "error_code": "ValueError"}
    }
    assert b["results"] == {ids[1]: {"status": "FILLED"}}
    assert c["results"] == a["results"]


def test_failed_checkpoint_after_adoption_replays_safely(database, setup, monkeypatch):
    data, spec, venue, _ = setup
    worker = ProtectionSupervisor(database, venue, scope=SCOPE)

    def fail(*args):
        raise RuntimeError("checkpoint unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(worker, "_record", fail)
        with pytest.raises(RuntimeError):
            worker.run_once()
    assert len(data.trace(spec.episode)["fills"]) == 2
    assert worker.run_once()["status"] == "SCANNED"
    assert len(data.trace(spec.episode)["fills"]) == 2


def test_other_account_state_not_scanned(database, setup):
    _, spec, venue, child = setup
    key = child.protection.key(spec)
    other = StateKey(**{**asdict(key), "account_id": "other"})
    child.protection.store.change(
        other,
        expected_version=0,
        request_key="test",
        payload={"spec": asdict(spec), "status": "SENDING"},
        reason="TEST",
    )
    result = ProtectionSupervisor(database, venue, scope=SCOPE).run_once()
    assert set(result["results"]) == {key.identity}


@pytest.mark.parametrize("limit", [0, 101, True, 1.5])
def test_limit_validated_before_network(database, setup, limit):
    _, _, venue, _ = setup
    with pytest.raises(ValueError):
        ProtectionSupervisor(database, venue, scope=SCOPE).run_once(limit)
    assert not venue.calls


def test_alert_schema_upgrade_is_repeatable_and_preserves_existing_events(database):
    script = (
        Path(__file__).resolve().parents[2]
        / "db/migrations/20260921_protection_recovery_alert.sql"
    ).read_text()
    with database() as conn:
        conn.execute(
            "ALTER TABLE v2_operational_outbox DROP CONSTRAINT v2_operational_outbox_event_type_check, ADD CONSTRAINT v2_operational_outbox_event_type_check CHECK(event_type IN ('MARKET_FAILURE','CANDLE_QUARANTINED','ACCOUNT_INVENTORY'))"
        )
        conn.execute(
            "INSERT INTO v2_operational_outbox(event_id,scope_id,dedup_key,event_type,payload) VALUES (gen_random_uuid(),'SANDBOX','existing','MARKET_FAILURE','{}')"
        )
        conn.execute(script)
        conn.execute(script)
        conn.execute(
            "INSERT INTO v2_operational_outbox(event_id,scope_id,dedup_key,event_type,payload) VALUES (gen_random_uuid(),'SANDBOX','new','PROTECTION_RECOVERY','{}')"
        )
        assert (
            conn.execute("SELECT count(*) FROM v2_operational_outbox").fetchone()[0]
            == 2
        )


def test_settled_completed_parent_no_longer_polled(database, setup):
    _, spec, venue, _ = setup
    worker = ProtectionSupervisor(database, venue, scope=SCOPE)
    worker.run_once()
    # Scheduling-only fixture, not evidence that this synthetic episode settled.
    with database() as conn:
        conn.execute(
            "UPDATE v2_episodes SET status='SETTLED' WHERE episode_id=%s",
            (spec.episode,),
        )
    venue.calls.clear()
    assert worker.run_once()["results"] == {}
    assert not venue.calls


def test_shutdown_does_not_advance_unprocessed_checkpoint(database, setup):
    _, _, venue, _ = setup
    worker = ProtectionSupervisor(database, venue, scope=SCOPE)
    assert worker.run_once(stop_requested=lambda: True)["results"] == {}
    assert worker.store.read(worker.cursor) is None
    assert not venue.calls


def test_telegram_protection_contract_filters_secrets():
    conn = TelegramConnection()
    assert sink(conn)(
        "e1",
        "SANDBOX",
        "PROTECTION_RECOVERY",
        {"state_id": "s1", "status": "UNPROTECTED", "secret": "NEVER_FORWARD"},
    )
    body = conn.calls[0][1]["body"]
    assert b"UNPROTECTED" in body and b"s1" in body and b"NEVER_FORWARD" not in body
