-- Additive registry foundation. Apply once inside a migration transaction.
-- Not an execution permit or an authentication layer. Existing history untouched.
CREATE TABLE v2_tenants (
    tenant_id UUID PRIMARY KEY,
    display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 120),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE v2_tenant_accounts (
    registry_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES v2_tenants(tenant_id),
    exchange TEXT NOT NULL CHECK (exchange='BINANCE'),
    account_id TEXT NOT NULL,
    environment TEXT NOT NULL CHECK (environment IN ('SANDBOX','LIVE')),
    product TEXT NOT NULL CHECK (product='FUTURES'),
    display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 120),
    credential_ref UUID NOT NULL,
    version BIGINT NOT NULL CHECK (version>0),
    status TEXT NOT NULL CHECK (status='ENROLLED_UNVERIFIED'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(exchange,account_id,environment,product),
    UNIQUE(tenant_id,registry_id)
);
CREATE TABLE v2_registry_events (
    event_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES v2_tenants(tenant_id),
    registry_id UUID NOT NULL,
    request_id UUID NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN ('ENROLL','ROTATE_CREDENTIAL')),
    request_digest TEXT NOT NULL,
    version BIGINT NOT NULL,
    result JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id),
    UNIQUE(tenant_id,request_id),
    UNIQUE(registry_id,version)
);
CREATE TRIGGER v2_registry_events_immutable BEFORE UPDATE OR DELETE ON v2_registry_events
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
