"""Internal AES-256-GCM vault. No public decrypt endpoint or file fallback.

key_provider(key_id) must resolve a separate protected master-key provider.
Server-authenticated authorization is required before calling this internal API.
Plaintext exists transiently in memory for signing; Python cannot promise zeroization.
"""

import hmac
import json
import os
import re

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from v2_core.account_registry import uid


class VaultError(RuntimeError):
    pass


def _secret(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9_+/=-]{16,512}", value
    ):
        raise VaultError("INVALID_CREDENTIAL_FORMAT")
    return value


class CredentialVault:
    def __init__(self, connect, *, key_provider, active_key_id):
        if not callable(key_provider) or not re.fullmatch(
            r"[A-Za-z0-9_-]{1,64}", active_key_id
        ):
            raise VaultError("VAULT_CONFIGURATION_REQUIRED")
        self.connect, self._provider, self.active_key_id = (
            connect,
            key_provider,
            active_key_id,
        )

    def _cipher(self, key_id):
        try:
            key = self._provider(key_id)
            if not isinstance(key, bytes) or len(key) != 32:
                raise ValueError()
            return AESGCM(key)
        except Exception:  # noqa: BLE001 - provider errors must never expose master material
            raise VaultError("MASTER_KEY_UNAVAILABLE") from None

    @staticmethod
    def _aad(tenant, ref, environment, key_id):
        if environment not in {"SANDBOX", "LIVE"}:
            raise VaultError("INVALID_CREDENTIAL_ENVIRONMENT")
        return json.dumps(
            ["trade-v2-vault-v1", tenant, ref, environment, key_id],
            separators=(",", ":"),
        ).encode()

    def put(self, tenant_id, credential_ref, *, environment, api_key, api_secret):
        tenant, ref = uid(tenant_id), uid(credential_ref)
        payload = json.dumps(
            [_secret(api_key), _secret(api_secret)], separators=(",", ":")
        ).encode()
        aad = self._aad(tenant, ref, environment, self.active_key_id)
        with self.connect() as c:
            c.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
                ("credential:" + ref,),
            )
            row = c.execute(
                "SELECT tenant_id::text,environment,key_id,nonce,ciphertext FROM v2_credential_vault WHERE credential_ref=%s",
                (ref,),
            ).fetchone()
            if row:
                if row[:2] != (tenant, environment):
                    raise VaultError("CREDENTIAL_REFERENCE_CONFLICT")
                plain = self._decrypt(tenant, ref, *row[1:])
                if not hmac.compare_digest(payload, plain):
                    raise VaultError("CREDENTIAL_REFERENCE_CONFLICT")
                return ref
            nonce = os.urandom(12)
            encrypted = self._cipher(self.active_key_id).encrypt(nonce, payload, aad)
            c.execute(
                "INSERT INTO v2_credential_vault(credential_ref,tenant_id,environment,key_id,nonce,ciphertext) VALUES (%s,%s,%s,%s,%s,%s)",
                (ref, tenant, environment, self.active_key_id, nonce, encrypted),
            )
        return ref

    def _decrypt(self, tenant, ref, environment, key_id, nonce, ciphertext):
        cipher = self._cipher(key_id)
        try:
            return cipher.decrypt(
                bytes(nonce),
                bytes(ciphertext),
                self._aad(tenant, ref, environment, key_id),
            )
        except Exception:  # noqa: BLE001 - integrity diagnostics cannot expose plaintext
            raise VaultError("CREDENTIAL_INTEGRITY_FAILURE") from None

    def resolve(self, tenant_id, credential_ref, *, environment):
        """Internal signing boundary only. NEVER exposed as HTTP response."""
        tenant, ref = uid(tenant_id), uid(credential_ref)
        with self.connect() as c:
            row = c.execute(
                "SELECT environment,key_id,nonce,ciphertext FROM v2_credential_vault WHERE tenant_id=%s AND credential_ref=%s AND environment=%s",
                (tenant, ref, environment),
            ).fetchone()
        if row is None:
            raise VaultError("CREDENTIAL_NOT_FOUND")
        try:
            key, secret = json.loads(self._decrypt(tenant, ref, *row))
            return {"api_key": _secret(key), "api_secret": _secret(secret)}
        except VaultError:
            raise
        except Exception:  # noqa: BLE001 - suppress decrypted payload parse details
            raise VaultError("CREDENTIAL_INTEGRITY_FAILURE") from None
