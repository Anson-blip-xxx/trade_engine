from pathlib import Path

import psycopg
import pytest
from test_v2_runtime_credentials import bound as bound_fixture
from test_v2_runtime_credentials import case as case_fixture
from test_v2_runtime_credentials import database as database_fixture
from test_v2_runtime_credentials import setup as setup_fixture

from services.v2_credential_acceptance import check_encrypted_credentials
from v2_core.credential_vault import VaultError
from v2_core.transport import ExchangeTransportError

database = database_fixture
setup = setup_fixture
case = case_fixture
bound = bound_fixture


@pytest.fixture
def prepared(bound):
    console, _, args = bound
    with console.connect() as c:
        c.execute(
            (
                Path(__file__).resolve().parents[2]
                / "db/migrations/20260927_credential_checks.sql"
            ).read_text()
        )
    return console, args


@pytest.mark.parametrize(
    "reply",
    [
        {"dualSidePosition": False},
        {"dualSidePosition": True},
        {"dualSidePosition": "false"},
        {},
        None,
        "failure",
    ],
)
def test_acceptance_get_only_and_safe_audit(prepared, reply):
    console, args = prepared
    calls = []

    def factory(**config):
        assert config["environment"] == "SANDBOX" and config["timeout"] == 10
        assert all(
            value is False for key, value in config.items() if key.startswith("enable_")
        )

        def request(method, path, params):
            calls.append((method, path, params))
            if reply == "failure":
                raise RuntimeError("secret key must not leak")
            return reply

        return request

    result = check_encrypted_credentials(
        console.connect,
        **args,
        permit=lambda *a: True,
        clock_ms=lambda: 1,
        transport_factory=factory,
    )
    assert calls == [("GET", "/fapi/v1/positionSide/dual", {})]
    expected = (
        "SIGNED_READ_ACCEPTED"
        if isinstance(reply, dict) and type(reply.get("dualSidePosition")) is bool
        else "SIGNED_READ_FAILED"
    )
    assert result["outcome"] == expected
    assert (
        not result["execution_authorized"]
        and not result["venue_identity_verified"]
        and not result["source_cleanup_authorized"]
    )
    with console.connect() as c:
        row = c.execute(
            "SELECT row_to_json(t)::text FROM v2_credential_checks t"
        ).fetchone()[0]
        assert "secret" not in row and "api_key" not in row
    with pytest.raises(psycopg.Error), console.connect() as c:
        c.execute("DELETE FROM v2_credential_checks")


def test_binding_failure_never_contacts_venue(prepared):
    console, args = prepared
    with pytest.raises(VaultError):
        check_encrypted_credentials(
            console.connect,
            **{**args, "binding_version": 2},
            permit=lambda *a: True,
            clock_ms=lambda: 1,
            transport_factory=lambda **kw: pytest.fail("venue touched"),
        )


def test_rejected_key_has_safe_diagnostic_receipt(prepared):
    console, args = prepared

    def factory(**kwargs):
        def request(*_):
            raise ExchangeTransportError(
                "EXCHANGE_RESPONSE_ERROR", status=401, code=-2015
            )

        return request

    result = check_encrypted_credentials(
        console.connect,
        **args,
        permit=lambda *a: True,
        clock_ms=lambda: 1,
        transport_factory=factory,
    )
    assert result["diagnostic"] == {
        "category": "EXCHANGE_RESPONSE_ERROR",
        "http_status": 401,
        "exchange_code": -2015,
    }
    assert result["outcome"] == "SIGNED_READ_FAILED"
    with console.connect() as c:
        assert (
            c.execute(
                "SELECT diagnostic FROM v2_credential_checks WHERE check_id=%s",
                (result["check_id"],),
            ).fetchone()[0]
            == result["diagnostic"]
        )
