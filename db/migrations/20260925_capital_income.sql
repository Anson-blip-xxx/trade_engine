-- Internal projection only. Requires tenant registry and capital journal.
CREATE TABLE v2_capital_baselines (
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    currency TEXT NOT NULL CHECK(currency ~ '^[A-Z][A-Z0-9]{0,19}$'),
    through_ms BIGINT NOT NULL CHECK(through_ms BETWEEN 0 AND 9007199254740991),
    opening_amount NUMERIC NOT NULL CHECK(opening_amount>=0 AND opening_amount<1e20
        AND opening_amount=trunc(opening_amount,18)),
    evidence_ref UUID NOT NULL,
    journal_id UUID UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,registry_id,currency),
    CHECK((opening_amount=0) = (journal_id IS NULL)),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id),
    FOREIGN KEY(tenant_id,journal_id) REFERENCES v2_capital_journals(tenant_id,journal_id)
);
CREATE TABLE v2_capital_income_receipts (
    income_id UUID PRIMARY KEY REFERENCES v2_exchange_income(income_id),
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    journal_id UUID NOT NULL UNIQUE,
    mapping_version TEXT NOT NULL CHECK(mapping_version='binance-cash-v1'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id),
    FOREIGN KEY(tenant_id,journal_id) REFERENCES v2_capital_journals(tenant_id,journal_id)
);
CREATE TABLE v2_capital_projection_runs (
    run_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    result JSONB NOT NULL CHECK(jsonb_typeof(result)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
CREATE TABLE v2_capital_wallet_checks (
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    observation_ref UUID NOT NULL,
    request_digest TEXT NOT NULL,
    result JSONB NOT NULL CHECK(jsonb_typeof(result)='object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(tenant_id,registry_id,observation_ref),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
CREATE TRIGGER v2_capital_wallet_checks_immutable BEFORE UPDATE OR DELETE ON v2_capital_wallet_checks
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE TRIGGER v2_capital_baselines_immutable BEFORE UPDATE OR DELETE ON v2_capital_baselines
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE TRIGGER v2_capital_income_receipts_immutable BEFORE UPDATE OR DELETE ON v2_capital_income_receipts
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE TRIGGER v2_capital_projection_runs_immutable BEFORE UPDATE OR DELETE ON v2_capital_projection_runs
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();

CREATE FUNCTION v2_guard_capital_baseline() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.opening_amount<>0 AND NOT EXISTS (
        SELECT 1 FROM v2_capital_journals j JOIN v2_capital_entries e USING(tenant_id,journal_id)
        WHERE j.journal_id=NEW.journal_id AND j.registry_id=NEW.registry_id
        AND j.kind='OPENING' AND j.currency=NEW.currency AND j.occurred_at_ms=NEW.through_ms
        AND e.registry_id=NEW.registry_id AND e.book='CASH' AND e.amount=NEW.opening_amount
    ) THEN RAISE EXCEPTION 'invalid capital baseline binding'; END IF;
    IF EXISTS (
        SELECT 1 FROM v2_capital_entries e JOIN v2_capital_journals j USING(tenant_id,journal_id)
        WHERE e.tenant_id=NEW.tenant_id AND e.registry_id=NEW.registry_id AND j.currency=NEW.currency
        AND j.journal_id IS DISTINCT FROM NEW.journal_id
    ) THEN RAISE EXCEPTION 'baseline requires empty currency book'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER v2_capital_baseline_binding BEFORE INSERT ON v2_capital_baselines
FOR EACH ROW EXECUTE FUNCTION v2_guard_capital_baseline();

CREATE FUNCTION v2_guard_capital_income_receipt() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM v2_exchange_income i
        JOIN v2_tenant_accounts a ON (a.exchange,a.account_id,a.environment,a.product)=
            (i.exchange,i.account_id,i.environment,i.product)
        JOIN v2_capital_baselines b ON (b.tenant_id,b.registry_id,b.currency)=(a.tenant_id,a.registry_id,i.currency)
        JOIN v2_capital_journals j ON j.journal_id=NEW.journal_id
        JOIN v2_capital_entries e ON e.journal_id=j.journal_id AND e.registry_id=a.registry_id AND e.book='CASH'
        WHERE i.income_id=NEW.income_id AND (a.tenant_id,a.registry_id)=(NEW.tenant_id,NEW.registry_id)
        AND (j.tenant_id,j.registry_id)=(NEW.tenant_id,NEW.registry_id)
        AND i.occurred_at_ms>b.through_ms AND j.occurred_at_ms=i.occurred_at_ms
        AND NOT EXISTS(SELECT 1 FROM v2_capital_journals undo WHERE undo.reverses=b.journal_id)
        AND j.currency=i.currency AND e.amount=i.amount
        AND j.source='binance-income-v1' AND j.source_id=i.income_id::text
        AND j.kind=CASE i.income_type WHEN 'REALIZED_PNL' THEN 'REALIZED_PNL'
            WHEN 'FUNDING_FEE' THEN 'FUNDING' WHEN 'COMMISSION' THEN 'FEE' END
    ) THEN RAISE EXCEPTION 'invalid capital income binding'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER v2_capital_income_binding BEFORE INSERT ON v2_capital_income_receipts
FOR EACH ROW EXECUTE FUNCTION v2_guard_capital_income_receipt();
