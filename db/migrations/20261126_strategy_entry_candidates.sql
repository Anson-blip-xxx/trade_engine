-- Additive offline typed-entry foundation. Apply once in a migration transaction.
-- A frozen candidate is not a receipt, an admission, or permission to trade.
CREATE TABLE v2_strategy_entry_candidates (
    consumer TEXT NOT NULL CHECK (length(consumer) BETWEEN 1 AND 1024),
    signal_id UUID NOT NULL REFERENCES v2_inbound_signals(signal_id),
    evidence_ref TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (consumer,signal_id),
    FOREIGN KEY (evidence_ref,signal_id)
      REFERENCES v2_decision_evidence(evidence_ref,signal_id)
);
CREATE TRIGGER v2_strategy_entry_candidates_immutable
BEFORE UPDATE OR DELETE ON v2_strategy_entry_candidates
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
