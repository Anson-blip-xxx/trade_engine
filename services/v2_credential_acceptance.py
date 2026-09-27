"""Bounded signed GET using decrypted Testnet material; no trading writes.

The receipt proves this logical binding completed a signed read, not distinct
venue identity, trading permission, worker ownership or source-cleanup safety.
"""

from contextlib import nullcontext
from uuid import uuid4

from psycopg.types.json import Jsonb

from v2_core.account_registry import uid
from v2_core.runtime_credentials import resolve_runtime_credentials
from v2_core.transport import BinanceSignedTransport, ExchangeTransportError


def check_encrypted_credentials(
    connect,
    *,
    tenant_id,
    registry_id,
    scope,
    binding_version,
    key_provider,
    permit,
    clock_ms,
    transport_factory=BinanceSignedTransport,
):
    tenant, registry = uid(tenant_id), uid(registry_id)
    with connect() as c:
        # Retain the registry SHARE lock through bounded I/O + immutable receipt.
        private = resolve_runtime_credentials(
            lambda: nullcontext(c),
            scope=scope,
            tenant_id=tenant,
            registry_id=registry,
            binding_version=binding_version,
            key_provider=key_provider,
        )
        outcome = "SIGNED_READ_FAILED"
        diagnostic = {"category": "INVALID_RESPONSE"}
        try:
            transport = transport_factory(
                account_id=scope.account_id,
                environment="SANDBOX",
                api_key=private["api_key"],
                api_secret=private["api_secret"],
                clock_ms=clock_ms,
                permit=permit,
                timeout=10,
                enable_trading=False,
                enable_testnet_cancellation=False,
                enable_testnet_order_cancellation=False,
                enable_testnet_protection=False,
                enable_testnet_settings=False,
            )
            response = transport("GET", "/fapi/v1/positionSide/dual", {})
            if (
                isinstance(response, dict)
                and type(response.get("dualSidePosition")) is bool
            ):
                outcome = "SIGNED_READ_ACCEPTED"
                diagnostic = {}
        except ExchangeTransportError as exc:
            diagnostic = exc.diagnostic_evidence()
        except Exception:  # noqa: BLE001 - never persist request/response or exception text
            outcome = "SIGNED_READ_FAILED"
            diagnostic = {"category": "INTERNAL_PROBE_ERROR"}
        check_id = str(uuid4())
        c.execute(
            "INSERT INTO v2_credential_checks(check_id,tenant_id,registry_id,binding_version,outcome,diagnostic) VALUES (%s,%s,%s,%s,%s,%s)",
            (check_id, tenant, registry, binding_version, outcome, Jsonb(diagnostic)),
        )
    return {
        "check_id": check_id,
        "outcome": outcome,
        "diagnostic": diagnostic,
        "execution_authorized": False,
        "venue_identity_verified": False,
        "source_cleanup_authorized": False,
    }
