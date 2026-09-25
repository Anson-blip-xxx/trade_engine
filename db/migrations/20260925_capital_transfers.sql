-- Internal attested transfer accounting. No venue transfer permission.
ALTER TABLE v2_capital_journals DROP CONSTRAINT v2_capital_journals_kind_check;
ALTER TABLE v2_capital_journals ADD CHECK(kind IN ('OPENING','DEPOSIT','WITHDRAWAL','REALIZED_PNL',
    'FEE','FEE_REBATE','FUNDING','TRANSFER','REVERSAL','TRANSFER_OUT','TRANSFER_IN'));
ALTER TABLE v2_capital_entries DROP CONSTRAINT v2_capital_entries_book_check;
ALTER TABLE v2_capital_entries ADD CHECK(book IN ('CASH','EXTERNAL_CAPITAL','REALIZED_PNL','FEES','FUNDING','TRANSFER_CLEARING'));

CREATE OR REPLACE FUNCTION v2_check_capital_journal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    j v2_capital_journals%ROWTYPE; old_j v2_capital_journals%ROWTYPE;
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
        OR NOT EXISTS (SELECT 1 FROM v2_capital_entries WHERE journal_id=j.journal_id AND registry_id=j.registry_id AND amount<0)
        THEN RAISE EXCEPTION 'invalid matched transfer'; END IF;
    ELSE
        expected_book := CASE WHEN j.kind IN ('OPENING','DEPOSIT','WITHDRAWAL') THEN 'EXTERNAL_CAPITAL'
            WHEN j.kind IN ('FEE','FEE_REBATE') THEN 'FEES'
            WHEN j.kind IN ('TRANSFER_OUT','TRANSFER_IN') THEN 'TRANSFER_CLEARING' ELSE j.kind END;
        IF EXISTS (SELECT 1 FROM v2_capital_entries WHERE journal_id=j.journal_id
                   AND (registry_id<>j.registry_id OR book NOT IN ('CASH',expected_book)))
        THEN RAISE EXCEPTION 'invalid capital books'; END IF;
        SELECT amount INTO STRICT cash FROM v2_capital_entries WHERE journal_id=j.journal_id AND book='CASH';
        IF (j.kind IN ('OPENING','DEPOSIT','FEE_REBATE','TRANSFER_IN') AND cash<0)
           OR (j.kind IN ('WITHDRAWAL','FEE','TRANSFER_OUT') AND cash>0)
        THEN RAISE EXCEPTION 'invalid cash direction'; END IF;
    END IF;
    RETURN NULL;
END $$;

CREATE TABLE v2_capital_transfer_pairs (
    pair_id UUID PRIMARY KEY,
    tenant_id UUID NOT NULL REFERENCES v2_tenants(tenant_id),
    outgoing_id UUID NOT NULL UNIQUE REFERENCES v2_exchange_income(income_id),
    incoming_id UUID NOT NULL UNIQUE REFERENCES v2_exchange_income(income_id),
    outgoing_journal UUID NOT NULL UNIQUE,
    incoming_journal UUID NOT NULL UNIQUE,
    evidence_ref UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(tenant_id,evidence_ref),
    FOREIGN KEY(tenant_id,outgoing_journal) REFERENCES v2_capital_journals(tenant_id,journal_id),
    FOREIGN KEY(tenant_id,incoming_journal) REFERENCES v2_capital_journals(tenant_id,journal_id)
);
CREATE TRIGGER v2_capital_transfer_pairs_immutable BEFORE UPDATE OR DELETE ON v2_capital_transfer_pairs
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();

CREATE FUNCTION v2_guard_capital_transfer_pair() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM v2_exchange_income o JOIN v2_exchange_income i ON i.income_id=NEW.incoming_id
        JOIN v2_tenant_accounts a ON (a.exchange,a.account_id,a.environment,a.product)=(o.exchange,o.account_id,o.environment,o.product)
        JOIN v2_tenant_accounts b ON (b.exchange,b.account_id,b.environment,b.product)=(i.exchange,i.account_id,i.environment,i.product)
        JOIN v2_capital_baselines ab ON (ab.tenant_id,ab.registry_id,ab.currency)=(a.tenant_id,a.registry_id,o.currency)
        JOIN v2_capital_baselines bb ON (bb.tenant_id,bb.registry_id,bb.currency)=(b.tenant_id,b.registry_id,i.currency)
        JOIN v2_capital_journals oj ON oj.journal_id=NEW.outgoing_journal
        JOIN v2_capital_journals ij ON ij.journal_id=NEW.incoming_journal
        JOIN v2_capital_entries oe ON oe.journal_id=oj.journal_id AND oe.book='CASH'
        JOIN v2_capital_entries ie ON ie.journal_id=ij.journal_id AND ie.book='CASH'
        WHERE o.income_id=NEW.outgoing_id AND o.income_type='TRANSFER' AND i.income_type='TRANSFER'
        AND o.amount<0 AND i.amount=-o.amount AND o.currency=i.currency
        AND (a.tenant_id,b.tenant_id)=(NEW.tenant_id,NEW.tenant_id) AND a.registry_id<>b.registry_id
        AND (a.exchange,a.environment,a.product)=(b.exchange,b.environment,b.product)
        AND o.occurred_at_ms<=i.occurred_at_ms AND o.occurred_at_ms>ab.through_ms AND i.occurred_at_ms>bb.through_ms
        AND NOT EXISTS(SELECT 1 FROM v2_capital_journals undo WHERE undo.reverses IN (ab.journal_id,bb.journal_id,oj.journal_id,ij.journal_id))
        AND (oj.tenant_id,oj.registry_id,oj.currency,oj.occurred_at_ms)=(a.tenant_id,a.registry_id,o.currency,o.occurred_at_ms)
        AND (ij.tenant_id,ij.registry_id,ij.currency,ij.occurred_at_ms)=(b.tenant_id,b.registry_id,i.currency,i.occurred_at_ms)
        AND oj.kind='TRANSFER_OUT' AND ij.kind='TRANSFER_IN' AND oe.amount=o.amount AND ie.amount=i.amount
        AND oj.source='binance-transfer-v1' AND ij.source='binance-transfer-v1'
        AND oj.source_id=o.income_id::text AND ij.source_id=i.income_id::text
    ) THEN RAISE EXCEPTION 'invalid capital transfer pair'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER v2_capital_transfer_pair_binding BEFORE INSERT ON v2_capital_transfer_pairs
FOR EACH ROW EXECUTE FUNCTION v2_guard_capital_transfer_pair();

-- One view lets the existing projector/checker observe both supported cash and
-- transfer receipts. Pair timestamps remain on their respective journal legs.
CREATE VIEW v2_capital_all_income_receipts AS
SELECT income_id,tenant_id,registry_id,journal_id,NULL::uuid AS related_journal_id FROM v2_capital_income_receipts
UNION ALL
SELECT p.outgoing_id,p.tenant_id,j.registry_id,p.outgoing_journal,p.incoming_journal
FROM v2_capital_transfer_pairs p JOIN v2_capital_journals j ON j.journal_id=p.outgoing_journal
UNION ALL
SELECT p.incoming_id,p.tenant_id,j.registry_id,p.incoming_journal,p.outgoing_journal
FROM v2_capital_transfer_pairs p JOIN v2_capital_journals j ON j.journal_id=p.incoming_journal;
