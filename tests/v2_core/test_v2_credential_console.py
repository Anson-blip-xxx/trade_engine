import json
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from test_v2_capital_journal import setup as journal_setup
from test_v2_intent_admission import database as database_fixture
from test_v2_intent_admission import intent

from services.v2_account_admin_http import handler_for
from services.v2_account_console import AccountConsole
from v2_core.account_draining import AccountDraining
from v2_core.credential_vault import CredentialVault, VaultError
from v2_core.intents import IntentStore

database = database_fixture
setup = journal_setup
KEY, SECRET = "qa-api-key-" + "A" * 24, "qa-api-secret-" + "B" * 32


@pytest.fixture
def case(setup):
    j, _, tenant, _other, *_ = setup
    with j.connect() as c:
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260926_credential_vault.sql"
            ).read_text()
        )
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260925_account_switches.sql"
            ).read_text()
        )
    master = os.urandom(32)
    provider = lambda _: master
    return (
        AccountConsole(
            j.connect,
            tenant_id=tenant,
            key_provider=provider,
            active_key_id="qa-master",
        ),
        CredentialVault(j.connect, key_provider=provider, active_key_id="qa-master"),
        setup,
        master,
    )


def add(console, **overrides):
    args = {
        "request_id": str(uuid4()),
        "alias": "QA account",
        "environment": "SANDBOX",
        "api_key": KEY,
        "api_secret": SECRET,
    }
    args.update(overrides)
    return console.add(**args)


def test_ciphertext_only_and_scoped_resolution(case):
    console, vault, setup, master = case
    result = add(console)
    with console.connect() as c:
        row = c.execute(
            "SELECT credential_ref::text,nonce,ciphertext FROM v2_credential_vault"
        ).fetchone()
        rendered = c.execute(
            "SELECT row_to_json(v)::text FROM v2_credential_vault v"
        ).fetchone()[0]
    assert (
        KEY not in rendered and SECRET not in rendered and master.hex() not in rendered
    )
    assert vault.resolve(console.tenant, row[0], environment="SANDBOX") == {
        "api_key": KEY,
        "api_secret": SECRET,
    }
    for tenant, env in [(setup[3], "SANDBOX"), (console.tenant, "LIVE")]:
        with pytest.raises(VaultError, match="NOT_FOUND"):
            vault.resolve(tenant, row[0], environment=env)
    listed = json.dumps(console.accounts())
    assert KEY not in listed and SECRET not in listed and row[0] not in listed
    assert result["execution_authorized"] is False
    with pytest.raises(psycopg.Error), console.connect() as c:
        c.execute("UPDATE v2_credential_vault SET ciphertext='x'")


@pytest.mark.parametrize(
    "mutate", ["tenant", "ref", "environment", "nonce", "ciphertext", "key_id"]
)
def test_aad_and_ciphertext_tamper_fail_closed(case, mutate):
    console, vault, _, _ = case
    add(console)
    with console.connect() as c:
        row = c.execute(
            "SELECT tenant_id::text,credential_ref::text,environment,key_id,nonce,ciphertext FROM v2_credential_vault"
        ).fetchone()
    args = dict(
        zip(
            ("tenant", "ref", "environment", "key_id", "nonce", "ciphertext"),
            row,
            strict=True,
        )
    )
    args[mutate] = {
        "tenant": str(uuid4()),
        "ref": str(uuid4()),
        "environment": "LIVE",
        "key_id": "different",
        "nonce": os.urandom(12),
        "ciphertext": bytes(row[5])[:-1] + bytes([row[5][-1] ^ 1]),
    }[mutate]
    with pytest.raises(VaultError, match="INTEGRITY"):
        vault._decrypt(**args)


def test_random_nonce_for_same_plaintext_and_atomic_enrollment(case):
    console, _, _, _ = case
    add(console)
    add(console)
    with console.connect() as c:
        assert c.execute(
            "SELECT count(DISTINCT nonce),count(DISTINCT ciphertext) FROM v2_credential_vault"
        ).fetchone() == (2, 2)
        c.execute("""CREATE FUNCTION qa_vault_fail() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'qa failure'; END $$;
          CREATE TRIGGER qa_fail BEFORE INSERT ON v2_registry_events FOR EACH ROW EXECUTE FUNCTION qa_vault_fail();""")
    with pytest.raises(psycopg.Error):
        add(console)
    with console.connect() as c:
        assert c.execute("SELECT count(*) FROM v2_credential_vault").fetchone()[0] == 2


def test_request_replay_concurrency_conflict_and_missing_master(case):
    console, _vault, _, _ = case
    request = str(uuid4())
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: add(console, request_id=request), range(8)))
    assert all(r == results[0] for r in results)
    with pytest.raises(VaultError, match="CONFLICT"):
        add(console, request_id=request, api_secret=SECRET + "X")
    with console.connect() as c:
        assert c.execute("SELECT count(*) FROM v2_credential_vault").fetchone()[0] == 1
    console.key_provider = lambda _: (_ for _ in ()).throw(
        RuntimeError("secret-master")
    )
    with pytest.raises(VaultError, match="^MASTER_KEY_UNAVAILABLE$"):
        add(console)


def test_wrong_master_and_failed_web_enrollment_never_reveal_secrets(case):
    console, vault, _, _ = case
    add(console)
    with console.connect() as c:
        ref = c.execute(
            "SELECT credential_ref::text FROM v2_credential_vault"
        ).fetchone()[0]
    wrong = CredentialVault(
        console.connect,
        key_provider=lambda _: os.urandom(32),
        active_key_id="qa-master",
    )
    with pytest.raises(VaultError, match="INTEGRITY"):
        wrong.resolve(console.tenant, ref, environment="SANDBOX")
    console.key_provider = lambda _: None
    status, result = http(
        console,
        "/api/accounts",
        {
            "request_id": str(uuid4()),
            "alias": "failure",
            "environment": "SANDBOX",
            "api_key": KEY,
            "api_secret": SECRET,
        },
    )
    assert status == 409
    assert KEY not in json.dumps(result) and SECRET not in json.dumps(result)
    assert vault.resolve(console.tenant, ref, environment="SANDBOX")["api_key"] == KEY


def test_concurrent_alias_edits_only_one_wins(case):
    console, *_ = case
    rid = add(console)["registry_id"]

    def run(n):
        try:
            return console.rename(
                rid, alias=f"Name {n}", expected_version=0, request_id=uuid4()
            )
        except ValueError as exc:
            assert str(exc) == "ALIAS_VERSION_CONFLICT"
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(run, range(8)))
    assert sum(r is not None for r in results) == 1


def test_alias_cas_audit_and_foreign_scope(case):
    console, _, setup, _ = case
    rid = add(console)["registry_id"]
    request = str(uuid4())
    args = {"alias": "Alias B", "expected_version": 0, "request_id": request}
    assert console.rename(rid, **args) == {"version": 1}
    assert console.rename(rid, **args) == {"version": 1}
    with pytest.raises(ValueError, match="CONFLICT"):
        console.rename(rid, alias="stale", expected_version=0, request_id=uuid4())
    with pytest.raises(ValueError, match="NOT_FOUND"):
        console.rename(
            setup[6], alias="foreign", expected_version=0, request_id=uuid4()
        )
    assert (
        next(a for a in console.accounts() if a["registry_id"] == rid)["alias"]
        == "Alias B"
    )
    with console.connect() as c:
        assert (
            c.execute(
                "SELECT version FROM v2_tenant_accounts WHERE registry_id=%s", (rid,)
            ).fetchone()[0]
            == 1
        )


def test_account_view_never_mixes_foreign_or_other_account(case):
    console, _, setup, _ = case
    _, _, _, _, a, b, foreign, _ = setup
    for name in ("A", "A", "B", "C"):
        IntentStore(console.connect).admit(replace(intent(), account_id=name))
    assert console.overview(a)["summary"]["trade_intents"] == 2
    assert console.overview(b)["summary"]["trade_intents"] == 1
    assert len(console.overview(a)["trades"]) == 2
    with pytest.raises(ValueError, match="NOT_FOUND"):
        console.overview(foreign)


def http(console, path, body=None, headers=None, peer="127.0.0.1"):
    cls = handler_for(
        console, authenticated_user="qa-owner", origin="https://console.invalid"
    )
    h = cls.__new__(cls)
    h.path = path
    h.client_address = (peer, 1234)
    raw = json.dumps(body).encode() if body is not None else b""
    h.rfile = BytesIO(raw)
    h.headers = {
        "X-V2-Authenticated-User": "qa-owner",
        "Origin": "https://console.invalid",
        "X-V2-Action": "account-management",
        "Content-Type": "application/json",
        "Content-Length": str(len(raw)),
        **(headers or {}),
    }
    h._send = lambda status, payload, mime="application/json": (status, payload)
    return h.do_POST() if body is not None else h.do_GET()


@pytest.mark.parametrize(
    "headers,peer",
    [
        ({"X-V2-Authenticated-User": "intruder"}, "127.0.0.1"),
        ({}, "192.0.2.1"),
        ({"Origin": "https://evil.invalid"}, "127.0.0.1"),
        ({"X-V2-Action": ""}, "127.0.0.1"),
    ],
)
def test_http_rejects_unauthenticated_and_csrf(case, headers, peer):
    console, *_ = case
    assert http(console, "/api/accounts", {}, headers, peer)[0] == 403


def test_http_add_list_alias_scope_and_no_secret_route(case):
    console, _, setup, _ = case
    body = {
        "request_id": str(uuid4()),
        "alias": "web qa",
        "environment": "SANDBOX",
        "api_key": KEY,
        "api_secret": SECRET,
    }
    status, result = http(console, "/api/accounts", body)
    assert (
        status == 201
        and KEY not in json.dumps(result)
        and SECRET not in json.dumps(result)
    )
    rid = result["registry_id"]
    assert http(console, f"/api/accounts/{rid}/overview")[0] == 200
    assert http(console, f"/api/accounts/{setup[6]}/overview")[0] == 404
    assert http(console, f"/api/accounts/{rid}/secret")[0] == 404
    assert http(console, "/api/accounts", {**body, "tenant_id": setup[3]})[0] == 400
    assert http(console, "/api/accounts", {**body, "environment": "LIVE"})[0] == 409
    assert http(console, "/api/accounts", {}, {"Content-Length": "9000"})[0] == 413
    assert (
        http(console, "/api/accounts", headers={"X-V2-Authenticated-User": ""})[0]
        == 403
    )
    assert (
        http(
            console,
            f"/api/accounts/{rid}/alias",
            {
                "alias": "new web alias",
                "expected_version": 0,
                "request_id": str(uuid4()),
            },
        )[0]
        == 200
    )
    assert http(console, "/accounts")[0] == 200
    assert http(console, "/accounts/app.js")[0] == 200


def test_http_response_disables_storage_and_embedding(case):
    console, *_ = case
    cls = handler_for(
        console, authenticated_user="qa-owner", origin="https://console.invalid"
    )
    handler = cls.__new__(cls)
    headers = {}
    handler.send_response = lambda status: headers.update(status=status)
    handler.send_header = lambda key, value: headers.update({key: value})
    handler.end_headers = lambda: None
    handler.wfile = BytesIO()
    handler._send(200, {"ok": True})
    assert headers["Cache-Control"] == "no-store"
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    assert "Access-Control-Allow-Origin" not in headers


def test_switch_api_only_prepares_and_no_source_drain(case):
    console, _, setup, _ = case
    status, result = http(
        console,
        "/api/account-switches",
        {
            "source_registry": setup[4],
            "target_registry": setup[5],
            "request_id": str(uuid4()),
        },
    )
    assert status == 202 and result["status"] == "REQUESTED"
    assert not result["target_activation_authorized"]
    assert (
        AccountDraining(console.connect).inspect(console.tenant, setup[4])["status"]
        == "NOT_DRAINING"
    )
