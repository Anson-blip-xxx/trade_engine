-- Logical removal preserves immutable account identity and financial history.
CREATE TABLE v2_account_retirements (
    tenant_id UUID NOT NULL,
    registry_id UUID PRIMARY KEY,
    reason TEXT NOT NULL CHECK (reason IN ('USER_REMOVED_INVALID_CREDENTIAL')),
    retired_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
CREATE TRIGGER v2_account_retirements_immutable BEFORE UPDATE OR DELETE ON v2_account_retirements
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
