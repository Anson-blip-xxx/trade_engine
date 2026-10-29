from dataclasses import asdict

from test_v2_protection_supervisor import SCOPE, database, setup

from v2_core.protection_supervisor import ProtectionSupervisor
from v2_core.state import StateKey

__all__ = ["database", "setup"]


def test_invalid_uuid_state_does_not_poison_valid_account_scan(database, setup):
    _, spec, venue, child = setup
    key = child.protection.key(spec)
    corrupt = StateKey(**{**asdict(key), "key": "malformed-episode"})
    child.protection.store.change(
        corrupt,
        expected_version=0,
        request_key="qa",
        payload={
            "spec": {**asdict(spec), "episode": "not-a-uuid"},
            "status": "SENDING",
        },
        reason="QA",
    )
    result = ProtectionSupervisor(database, venue, scope=SCOPE).run_once()
    assert set(result["results"]) == {key.identity}
