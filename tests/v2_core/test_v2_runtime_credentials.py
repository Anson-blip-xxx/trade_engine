import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_v2_credential_console import KEY, SECRET
from test_v2_credential_console import case as console_case
from test_v2_credential_console import database as database_fixture
from test_v2_credential_console import setup as journal_setup

from services.v2_testnet_daemon_entry import daemon_credentials, process_credentials
from v2_core.account_risk import AccountScope
from v2_core.credential_vault import VaultError
from v2_core.runtime_credentials import resolve_runtime_credentials
from v2_core.systemd_master_keys import SystemdMasterKeys, secure_mount

database = database_fixture
setup = journal_setup
case = console_case


def test_retired_account_hidden_and_cannot_resolve(bound):
    console, _, args = bound
    with console.connect() as c:
        c.execute(
            "INSERT INTO v2_account_retirements(tenant_id,registry_id,reason) VALUES (%s,%s,'USER_REMOVED_INVALID_CREDENTIAL')",
            (console.tenant, args["registry_id"]),
        )
    assert args["registry_id"] not in {a["registry_id"] for a in console.accounts()}
    with pytest.raises(VaultError, match="ACCOUNT_RETIRED"):
        resolve_runtime_credentials(console.connect, **args)
    # Historical statistics remain addressable; no financial history is deleted.
    assert console.overview(args["registry_id"])["registry_id"] == args["registry_id"]


@pytest.fixture
def bound(case):
    console, vault, fixture, _ = case
    registry = fixture[4]
    with console.connect() as c:
        ref = c.execute(
            "SELECT credential_ref::text FROM v2_tenant_accounts WHERE registry_id=%s",
            (registry,),
        ).fetchone()[0]
    vault.put(
        console.tenant, ref, environment="SANDBOX", api_key=KEY, api_secret=SECRET
    )
    return (
        console,
        fixture,
        {
            "scope": AccountScope("BINANCE", "A", "SANDBOX", "FUTURES"),
            "tenant_id": console.tenant,
            "registry_id": registry,
            "binding_version": 1,
            "key_provider": console.key_provider,
        },
    )


def test_pinned_scope_resolves_and_rotation_invalidates_startup(bound):
    console, fixture, args = bound
    assert resolve_runtime_credentials(console.connect, **args) == {
        "api_key": KEY,
        "api_secret": SECRET,
    }
    fixture[1].rotate_credential(
        console.tenant,
        args["registry_id"],
        credential_ref=uuid4(),
        expected_version=1,
        request_id=uuid4(),
    )
    with pytest.raises(VaultError, match="BINDING_MISMATCH"):
        resolve_runtime_credentials(console.connect, **args)


@pytest.mark.parametrize("change", ["tenant", "registry", "account", "version", "live"])
def test_scope_mismatch_never_reads_master(bound, change):
    console, fixture, args = bound

    def forbidden(_):
        pytest.fail("mismatched scope accessed master")

    args["key_provider"] = forbidden
    if change == "tenant":
        args["tenant_id"] = fixture[3]
    if change == "registry":
        args["registry_id"] = fixture[5]
    if change == "account":
        args["scope"] = replace(args["scope"], account_id="other")
    if change == "version":
        args["binding_version"] = 2
    if change == "live":
        args["scope"] = replace(args["scope"], environment="LIVE")
    with pytest.raises(VaultError):
        resolve_runtime_credentials(console.connect, **args)


def credential_file(tmp_path, monkeypatch):
    monkeypatch.setattr(SystemdMasterKeys, "ROOT", tmp_path)
    directory = tmp_path / "qa.service"
    directory.mkdir(mode=0o700)
    key = directory / "v2-master-v1"
    key.write_bytes(os.urandom(32))
    key.chmod(0o400)
    return directory, key


def test_provider_reads_exact_material_only_on_secure_mount(tmp_path, monkeypatch):
    directory, key = credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr("v2_core.systemd_master_keys.secure_mount", lambda fd: True)
    provider = SystemdMasterKeys(directory)
    assert provider("v1") == key.read_bytes()
    for key_id in ("../v1", "", None):
        with pytest.raises(VaultError):
            provider(key_id)


@pytest.mark.parametrize(
    "defect", ["disk", "mode", "short", "long", "symlink", "directory", "missing"]
)
def test_provider_rejects_unsafe_files(tmp_path, monkeypatch, defect):
    directory, key = credential_file(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "v2_core.systemd_master_keys.secure_mount", lambda fd: defect != "disk"
    )
    if defect == "mode":
        key.chmod(0o600)
    if defect in {"short", "long"}:
        key.chmod(0o600)
        key.write_bytes(b"x" * (31 if defect == "short" else 33))
        key.chmod(0o400)
    if defect == "symlink":
        target = tmp_path / "other"
        target.write_bytes(b"x" * 32)
        key.unlink()
        key.symlink_to(target)
    if defect == "directory":
        directory.chmod(0o755)
    if defect == "missing":
        key.unlink()
    with pytest.raises(VaultError, match="^MASTER_KEY_UNAVAILABLE$"):
        SystemdMasterKeys(directory)("v1")


def test_mount_detector_rejects_real_disk(tmp_path):
    location = tmp_path / "not-a-secret"
    location.write_bytes(b"fixture")
    with location.open("rb") as handle:
        assert secure_mount(handle.fileno()) is False


@pytest.mark.parametrize(
    "mount,expected",
    [
        ("42 1 0:1 / /run/credentials/qa ro - ramfs ramfs rw", True),
        ("42 1 0:1 / /run/credentials/qa ro - tmpfs tmpfs rw,noswap", True),
        ("42 1 0:1 / /run/credentials/qa ro - tmpfs tmpfs rw", False),
        ("42 1 0:1 / /run/credentials/qa rw - ramfs ramfs rw", False),
        ("42 1 0:1 / /run/credentials/qa ro - ext4 disk rw", False),
        ("43 1 0:1 / /run/credentials/qa ro - ramfs ramfs rw", False),
    ],
)
def test_mount_identity_and_noswap_contract(monkeypatch, mount, expected):
    monkeypatch.setattr(
        Path, "read_text", lambda p: "mnt_id:\t42\n" if "fdinfo" in str(p) else mount
    )
    assert secure_mount(123) is expected


def test_daemon_vault_mode_uses_notification_file_only(bound, tmp_path, monkeypatch):
    console, _, args = bound
    location = tmp_path / "notifications.env"
    location.write_text(
        "TG_NOTIFY_TOKEN=123:abcdefghijklmnop\nTG_NOTIFY_CHAT_ID=-123\n"
    )
    location.chmod(0o600)
    values = {
        "V2_CREDENTIAL_SOURCE": "vault",
        "V2_VAULT_TENANT_ID": console.tenant,
        "V2_VAULT_REGISTRY_ID": args["registry_id"],
        "V2_VAULT_BINDING_VERSION": "1",
        "CREDENTIALS_DIRECTORY": "/run/credentials/qa.service",
    }
    monkeypatch.setattr(
        "v2_core.systemd_master_keys.SystemdMasterKeys", lambda _: console.key_provider
    )
    config = SimpleNamespace(daemon=SimpleNamespace(account_id="A"))
    result = process_credentials(
        location, values=values, config=config, connect=console.connect
    )
    assert (
        result["BINANCE_TESTNET_API_KEY"] == KEY
        and result["BINANCE_TESTNET_API_SECRET"] == SECRET
    )
    assert result["TG_NOTIFY_CHAT_ID"] == "-123"
    # Even with valid legacy keys present, a broken vault must never use them.
    location.write_text(
        "BINANCE_TESTNET_API_KEY=legacy-key\nBINANCE_TESTNET_API_SECRET=legacy-secret\nTG_NOTIFY_TOKEN=123:abcdefghijklmnop\nTG_NOTIFY_CHAT_ID=-123\n"
    )
    console.key_provider = lambda _: None
    with pytest.raises(VaultError, match="MASTER_KEY_UNAVAILABLE"):
        process_credentials(
            location, values=values, config=config, connect=console.connect
        )


@pytest.mark.parametrize(
    "values",
    [
        {"V2_CREDENTIAL_SOURCE": "invalid"},
        {"V2_VAULT_TENANT_ID": "present"},
        {"V2_CREDENTIAL_SOURCE": "vault", "V2_VAULT_BINDING_VERSION": "0"},
        {"V2_CREDENTIAL_SOURCE": "vault", "V2_VAULT_BINDING_VERSION": "NaN"},
    ],
)
def test_invalid_config_does_not_touch_legacy_file(values):
    with pytest.raises((ValueError, KeyError, VaultError)):
        process_credentials(
            "/not/a/credential/file",
            values=values,
            config=SimpleNamespace(daemon=SimpleNamespace(account_id="A")),
            connect=lambda: pytest.fail("unexpected database read"),
        )


def test_vault_notification_file_cannot_hide_legacy_exchange_keys(tmp_path):
    location = tmp_path / "mixed.env"
    location.write_text(
        "TG_NOTIFY_TOKEN=123:abcdefghijklmnop\nTG_NOTIFY_CHAT_ID=-123\nBINANCE_TESTNET_API_KEY=qa-only\n"
    )
    location.chmod(0o600)
    with pytest.raises(
        ValueError, match="NOTIFICATION_FILE_CONTAINS_EXCHANGE_CREDENTIALS"
    ):
        daemon_credentials(location, notification_only=True)
