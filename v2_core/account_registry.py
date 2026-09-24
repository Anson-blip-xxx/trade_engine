"""Internal enrollment foundation: no secret reads, exchange I/O or activation.

Tenant IDs must ultimately come from authenticated server context. A supplied
tenant UUID is NOT authentication. This module is not exposed as a public API.
Credential references are opaque UUIDs, never API key/secret material.
"""

from dataclasses import asdict
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from v2_core.account_risk import AccountScope
from v2_core.evidence import canonical, digest


def uid(value):
    return str(UUID(str(value)))


def label(value):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 120
        or value != value.strip()
        or any(ord(c) < 32 for c in value)
    ):
        raise ValueError("INVALID_REGISTRY_LABEL")
    return value


class AccountRegistry:
    def __init__(self, connect):
        self.connect = connect

    def create_tenant(self, tenant_id, display_name):
        tenant_id, display_name = uid(tenant_id), label(display_name)
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO v2_tenants(tenant_id,display_name) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (tenant_id, display_name),
            )
            saved = conn.execute(
                "SELECT display_name FROM v2_tenants WHERE tenant_id=%s", (tenant_id,)
            ).fetchone()
            if saved[0] != display_name:
                raise ValueError("TENANT_IDENTITY_CONFLICT")
        return tenant_id

    def _begin(self, conn, tenant_id, request_id, request):
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            ("tenant-registry:" + tenant_id,),
        )
        if not conn.execute(
            "SELECT 1 FROM v2_tenants WHERE tenant_id=%s", (tenant_id,)
        ).fetchone():
            raise ValueError("TENANT_NOT_FOUND")
        fingerprint = digest(canonical(request))
        prior = conn.execute(
            "SELECT request_digest,result FROM v2_registry_events WHERE tenant_id=%s AND request_id=%s",
            (tenant_id, request_id),
        ).fetchone()
        if prior and prior[0] != fingerprint:
            raise ValueError("REGISTRY_REQUEST_CONFLICT")
        return fingerprint, prior[1] if prior else None

    @staticmethod
    def _record(conn, tenant, request_id, operation, fingerprint, result):
        conn.execute(
            """INSERT INTO v2_registry_events(event_id,tenant_id,registry_id,request_id,
            operation,request_digest,version,result) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
            (
                str(uuid4()),
                tenant,
                result["registry_id"],
                request_id,
                operation,
                fingerprint,
                result["version"],
                Jsonb(result),
            ),
        )

    def enroll(self, tenant_id, scope, *, display_name, credential_ref, request_id):
        if not isinstance(scope, AccountScope):
            raise TypeError("ACCOUNT_SCOPE_REQUIRED")
        tenant, request_id, credential = (
            uid(tenant_id),
            uid(request_id),
            uid(credential_ref),
        )
        request = {
            "operation": "ENROLL",
            "scope": asdict(scope),
            "display_name": label(display_name),
            "credential_ref": credential,
        }
        with self.connect() as conn:
            fingerprint, prior = self._begin(conn, tenant, request_id, request)
            if prior is not None:
                return prior
            registry_id = str(uuid4())
            inserted = conn.execute(
                """INSERT INTO v2_tenant_accounts(registry_id,tenant_id,exchange,account_id,
                environment,product,display_name,credential_ref,version,status)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,1,'ENROLLED_UNVERIFIED')
                ON CONFLICT (exchange,account_id,environment,product) DO NOTHING RETURNING registry_id""",
                (
                    registry_id,
                    tenant,
                    *asdict(scope).values(),
                    display_name,
                    credential,
                ),
            ).fetchone()
            if not inserted:
                raise ValueError("ACCOUNT_SCOPE_ALREADY_ENROLLED")
            result = {
                "registry_id": registry_id,
                "tenant_id": tenant,
                "scope": asdict(scope),
                "credential_ref": credential,
                "version": 1,
                "status": "ENROLLED_UNVERIFIED",
                "execution_authorized": False,
            }
            self._record(conn, tenant, request_id, "ENROLL", fingerprint, result)
            return result

    def get(self, tenant_id, registry_id):
        with self.connect() as conn:
            row = conn.execute(
                """SELECT registry_id::text,exchange,account_id,environment,product,
                credential_ref::text,version,status FROM v2_tenant_accounts WHERE tenant_id=%s AND registry_id=%s""",
                (uid(tenant_id), uid(registry_id)),
            ).fetchone()
        return (
            None
            if row is None
            else dict(
                zip(
                    (
                        "registry_id",
                        "exchange",
                        "account_id",
                        "environment",
                        "product",
                        "credential_ref",
                        "version",
                        "status",
                    ),
                    row,
                    strict=True,
                )
            )
        )

    def rotate_credential(
        self, tenant_id, registry_id, *, credential_ref, expected_version, request_id
    ):
        tenant, registry, credential, request_id = map(
            uid, (tenant_id, registry_id, credential_ref, request_id)
        )
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("REGISTRY_VERSION_REQUIRED")
        request = {
            "operation": "ROTATE_CREDENTIAL",
            "registry_id": registry,
            "credential_ref": credential,
            "expected_version": expected_version,
        }
        with self.connect() as conn:
            fingerprint, prior = self._begin(conn, tenant, request_id, request)
            if prior is not None:
                return prior
            row = conn.execute(
                """UPDATE v2_tenant_accounts SET credential_ref=%s,version=version+1
                WHERE tenant_id=%s AND registry_id=%s AND version=%s AND status='ENROLLED_UNVERIFIED'
                RETURNING version""",
                (credential, tenant, registry, expected_version),
            ).fetchone()
            if row is None:
                raise ValueError("ACCOUNT_NOT_FOUND_OR_VERSION_CONFLICT")
            result = {
                "registry_id": registry,
                "tenant_id": tenant,
                "credential_ref": credential,
                "version": row[0],
                "status": "ENROLLED_UNVERIFIED",
                "execution_authorized": False,
            }
            self._record(
                conn, tenant, request_id, "ROTATE_CREDENTIAL", fingerprint, result
            )
            return result
