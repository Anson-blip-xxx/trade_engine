-- Internal journal foundation; requires tenant_registry. Apply transactionally.
-- No execution authority, exchange transfer, or spendable-balance projection.
CREATE FUNCTION v2_registry_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'immutable account identity'; END IF;
    IF (to_jsonb(NEW) - ARRAY['credential_ref','version']) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['credential_ref','version']) OR NEW.version <> OLD.version+1
    THEN RAISE EXCEPTION 'immutable account identity'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER v2_registry_identity_immutable BEFORE UPDATE OR DELETE ON v2_tenant_accounts
FOR EACH ROW EXECUTE FUNCTION v2_registry_identity_guard();

CREATE TABLE v2_capital_journals (
    journal_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('OPENING','DEPOSIT','WITHDRAWAL','REALIZED_PNL',
        'FEE','FEE_REBATE','FUNDING','TRANSFER','REVERSAL')),
    currency TEXT NOT NULL CHECK(currency ~ '^[A-Z][A-Z0-9]{0,19}$'),
    source TEXT NOT NULL CHECK(source ~ '^[A-Za-z0-9_.:-]{1,120}$'),
    source_id TEXT NOT NULL CHECK(source_id ~ '^[A-Za-z0-9_.:-]{1,160}$'),
    request_digest TEXT NOT NULL CHECK(request_digest ~ '^[a-f0-9]{64}$'),
    occurred_at_ms BIGINT NOT NULL CHECK(occurred_at_ms BETWEEN 0 AND 9007199254740991),
    reverses UUID UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK((kind='REVERSAL') = (reverses IS NOT NULL)),
    UNIQUE(tenant_id,journal_id),
    UNIQUE(tenant_id,registry_id,kind,source,source_id),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id),
    FOREIGN KEY(tenant_id,reverses) REFERENCES v2_capital_journals(tenant_id,journal_id)
);
CREATE TABLE v2_capital_entries (
    journal_id UUID NOT NULL,
    tenant_id UUID NOT NULL,
    registry_id UUID NOT NULL,
    book TEXT NOT NULL CHECK(book IN ('CASH','EXTERNAL_CAPITAL','REALIZED_PNL','FEES','FUNDING')),
    amount NUMERIC NOT NULL CHECK(amount <> 0 AND abs(amount)<1e20
        AND amount=trunc(amount,18)),
    PRIMARY KEY(journal_id,registry_id,book),
    FOREIGN KEY(tenant_id,journal_id) REFERENCES v2_capital_journals(tenant_id,journal_id),
    FOREIGN KEY(tenant_id,registry_id) REFERENCES v2_tenant_accounts(tenant_id,registry_id)
);
CREATE INDEX v2_capital_account_entries ON v2_capital_entries(tenant_id,registry_id);
CREATE TRIGGER v2_capital_journals_immutable BEFORE UPDATE OR DELETE ON v2_capital_journals
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE TRIGGER v2_capital_entries_immutable BEFORE UPDATE OR DELETE ON v2_capital_entries
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();

CREATE FUNCTION v2_check_capital_journal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    j v2_capital_journals%ROWTYPE;
    old_j v2_capital_journals%ROWTYPE;
    n BIGINT; total NUMERIC; cash NUMERIC; expected_book TEXT;
BEGIN
    SELECT * INTO STRICT j FROM v2_capital_journals WHERE journal_id=NEW.journal_id;
    SELECT count(*),sum(amount) INTO n,total FROM v2_capital_entries WHERE journal_id=j.journal_id;
    IF n<>2 OR total<>0 THEN RAISE EXCEPTION 'capital journal must have two balanced entries'; END IF;
    IF EXISTS (
        SELECT 1 FROM v2_capital_entries e JOIN v2_tenant_accounts a USING(tenant_id,registry_id)
        JOIN v2_tenant_accounts anchor ON anchor.registry_id=j.registry_id
        WHERE e.journal_id=j.journal_id AND
        (a.exchange,a.environment,a.product) IS DISTINCT FROM (anchor.exchange,anchor.environment,anchor.product)
    ) THEN RAISE EXCEPTION 'capital scope mismatch'; END IF;
    IF j.kind='REVERSAL' THEN
        SELECT * INTO STRICT old_j FROM v2_capital_journals WHERE journal_id=j.reverses;
        IF old_j.kind='REVERSAL' OR old_j.currency<>j.currency OR old_j.registry_id<>j.registry_id
           OR old_j.occurred_at_ms>j.occurred_at_ms OR EXISTS (
            SELECT 1 FROM v2_capital_entries e WHERE e.journal_id=j.journal_id AND NOT EXISTS (
                SELECT 1 FROM v2_capital_entries o WHERE o.journal_id=j.reverses
                AND o.registry_id=e.registry_id AND o.book=e.book AND o.amount=-e.amount
            )
        ) THEN RAISE EXCEPTION 'invalid exact reversal'; END IF;
    ELSIF j.kind='TRANSFER' THEN
        IF EXISTS (SELECT 1 FROM v2_capital_entries WHERE journal_id=j.journal_id AND book<>'CASH')
        OR NOT EXISTS (SELECT 1 FROM v2_capital_entries WHERE journal_id=j.journal_id
                       AND registry_id=j.registry_id AND amount<0)
        THEN RAISE EXCEPTION 'invalid matched transfer'; END IF;
    ELSE
        expected_book := CASE WHEN j.kind IN ('OPENING','DEPOSIT','WITHDRAWAL') THEN 'EXTERNAL_CAPITAL'
            WHEN j.kind IN ('FEE','FEE_REBATE') THEN 'FEES' ELSE j.kind END;
        IF EXISTS (SELECT 1 FROM v2_capital_entries WHERE journal_id=j.journal_id
                   AND (registry_id<>j.registry_id OR book NOT IN ('CASH',expected_book)))
        THEN RAISE EXCEPTION 'invalid capital books'; END IF;
        SELECT amount INTO STRICT cash FROM v2_capital_entries WHERE journal_id=j.journal_id AND book='CASH';
        IF (j.kind IN ('OPENING','DEPOSIT','FEE_REBATE') AND cash<0)
           OR (j.kind IN ('WITHDRAWAL','FEE') AND cash>0)
        THEN RAISE EXCEPTION 'invalid cash direction'; END IF;
    END IF;
    RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER v2_capital_header_balanced AFTER INSERT ON v2_capital_journals
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION v2_check_capital_journal();
CREATE CONSTRAINT TRIGGER v2_capital_entries_balanced AFTER INSERT ON v2_capital_entries
DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION v2_check_capital_journal();
