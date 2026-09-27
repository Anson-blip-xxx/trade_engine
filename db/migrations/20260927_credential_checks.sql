-- Signed read acceptance only, never an execution/venue-identity authorization.
CREATE TABLE v2_credential_checks (
    check_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    binding_version BIGINT NOT NULL CHECK(binding_version>0),
    outcome TEXT NOT NULL CHECK(outcome IN ('SIGNED_READ_ACCEPTED','SIGNED_READ_FAILED')),
    diagnostic JSONB NOT NULL DEFAULT '{}' CHECK(jsonb_typeof(diagnostic)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
CREATE TRIGGER v2_credential_checks_immutable BEFORE UPDATE OR DELETE ON v2_credential_checks
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
