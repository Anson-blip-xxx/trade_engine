-- Requires tenant_registry; no plaintext API fields, no execution activation.
CREATE TABLE v2_credential_vault (
    credential_ref UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES v2_tenants(tenant_id),
    environment TEXT NOT NULL CHECK(environment IN ('SANDBOX','LIVE')),
    key_id TEXT NOT NULL CHECK(key_id ~ '^[A-Za-z0-9_-]{1,64}$'),
    nonce BYTEA NOT NULL CHECK(octet_length(nonce)=12),
    ciphertext BYTEA NOT NULL CHECK(octet_length(ciphertext) BETWEEN 32 AND 4096),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(key_id,nonce),
    UNIQUE(tenant_id,credential_ref)
);
CREATE TRIGGER v2_credential_vault_immutable BEFORE UPDATE OR DELETE ON v2_credential_vault
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE TABLE v2_account_aliases (
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    alias TEXT NOT NULL CHECK(length(alias) BETWEEN 1 AND 120),
    version BIGINT NOT NULL CHECK(version>0),
    PRIMARY KEY(tenant_id,registry_id),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
CREATE TABLE v2_account_alias_events (
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    request_id UUID NOT NULL,
    alias TEXT NOT NULL,
    version BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,request_id),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
CREATE TRIGGER v2_account_alias_events_immutable BEFORE UPDATE OR DELETE ON v2_account_alias_events
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
