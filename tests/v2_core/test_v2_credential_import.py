import os
import stat
import struct
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_credential_console import KEY, SECRET
from test_v2_credential_console import case as console_case
from test_v2_credential_console import database as database_fixture
from test_v2_credential_console import setup as journal_setup
from test_v2_intent_admission import intent

from v2_core.account_risk import AccountScope
from v2_core.credential_import import import_existing_credentials
from v2_core.credential_vault import VaultError
from v2_core.intents import IntentStore
from v2_core.runtime_credentials import resolve_runtime_credentials
from v2_core.systemd_master_keys import SystemdMasterKeys

database = database_fixture
setup = journal_setup
case = console_case


def arguments(console, **changes):
    args = {
        "tenant_id": console.tenant,
        "scope": AccountScope("BINANCE", "historical", "SANDBOX", "FUTURES"),
        "alias": "Imported QA",
        "request_id": str(uuid4()),
        "api_key": KEY,
        "api_secret": SECRET,
        "key_provider": console.key_provider,
        "active_key_id": "qa-master",
    }
    args.update(changes)
    return args


def test_import_preserves_history_and_decrypts_for_same_runtime(case):
    console, _, _, _ = case
    old = replace(intent(), account_id="historical")
    IntentStore(console.connect).admit(old)
    result = import_existing_credentials(console.connect, **arguments(console))
    report = console.overview(result["registry_id"])
    assert report["summary"]["trade_intents"] == 1
    assert report["trades"][0]["id"] == old.intent_id
    assert (
        not result["execution_authorized"] and not result["source_cleanup_authorized"]
    )
    material = resolve_runtime_credentials(
        console.connect,
        scope=AccountScope("BINANCE", "historical", "SANDBOX", "FUTURES"),
        tenant_id=console.tenant,
        registry_id=result["registry_id"],
        binding_version=1,
        key_provider=console.key_provider,
    )
    assert material == {"api_key": KEY, "api_secret": SECRET}


def test_import_concurrent_replay_and_changed_secret_rejected(case):
    console, *_ = case
    args = arguments(console)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda _: import_existing_credentials(console.connect, **args), range(8)
            )
        )
    assert all(r == results[0] for r in results)
    with pytest.raises(VaultError, match="CONFLICT"):
        import_existing_credentials(
            console.connect, **{**args, "api_secret": SECRET + "X"}
        )
    with console.connect() as c:
        assert c.execute("SELECT count(*) FROM v2_credential_vault").fetchone()[0] == 1


@pytest.mark.parametrize("name", ["A", "C"])
def test_existing_registry_is_not_rebound_or_adopted(case, name):
    console, *_ = case
    with pytest.raises(ValueError, match="ALREADY_ENROLLED"):
        import_existing_credentials(
            console.connect,
            **arguments(
                console, scope=AccountScope("BINANCE", name, "SANDBOX", "FUTURES")
            ),
        )
    with console.connect() as c:
        assert c.execute("SELECT count(*) FROM v2_credential_vault").fetchone()[0] == 0


def test_failed_readback_rolls_back_enrollment_and_ciphertext(case):
    console, *_ = case
    calls = []

    def provider(key_id):
        calls.append(key_id)
        return console.key_provider(key_id) if len(calls) == 1 else None

    with pytest.raises(VaultError, match="MASTER_KEY_UNAVAILABLE"):
        import_existing_credentials(
            console.connect, **arguments(console, key_provider=provider)
        )
    with console.connect() as c:
        assert c.execute("SELECT count(*) FROM v2_credential_vault").fetchone()[0] == 0
        assert (
            c.execute(
                "SELECT count(*) FROM v2_tenant_accounts WHERE account_id='historical'"
            ).fetchone()[0]
            == 0
        )


def test_live_import_denied_before_database_access(case):
    console, *_ = case
    with pytest.raises(VaultError, match="SANDBOX_IMPORT_ONLY"):
        import_existing_credentials(
            lambda: pytest.fail("database called"),
            **arguments(
                console, scope=AccountScope("BINANCE", "live", "LIVE", "FUTURES")
            ),
        )


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize(
    "defect",
    [
        None,
        "other-user",
        "group-read",
        "other-read",
        "write",
        "missing",
        "duplicate",
        "version",
    ],
)
def test_only_exact_systemd_service_user_acl_is_accepted(directory, defect):
    permission = 5 if directory else 4
    entries = [
        (1, permission, 0xFFFFFFFF),
        (2, permission, os.geteuid()),
        (4, 0, 0xFFFFFFFF),
        (16, permission, 0xFFFFFFFF),
        (32, 0, 0xFFFFFFFF),
    ]
    if defect == "other-user":
        entries[1] = (2, permission, os.geteuid() + 1)
    if defect == "group-read":
        entries[2] = (4, 4, 0xFFFFFFFF)
    if defect == "other-read":
        entries[4] = (32, 4, 0xFFFFFFFF)
    if defect == "write":
        entries[1] = (2, permission | 2, os.geteuid())
    if defect == "missing":
        entries.pop()
    if defect == "duplicate":
        entries.append(entries[1])
    acl = struct.pack("<I", 3 if defect == "version" else 2) + b"".join(
        struct.pack("<HHI", *r) for r in entries
    )
    details = SimpleNamespace(
        st_mode=(stat.S_IFDIR | 0o550) if directory else (stat.S_IFREG | 0o440),
        st_uid=0,
        st_gid=0,
    )
    assert SystemdMasterKeys._protected(details, directory=directory, acl=acl) == (
        defect is None
    )


def test_plain_group_access_without_service_acl_is_rejected():
    details = SimpleNamespace(
        st_mode=stat.S_IFREG | 0o440, st_uid=0, st_gid=os.getegid()
    )
    assert not SystemdMasterKeys._protected(details)
