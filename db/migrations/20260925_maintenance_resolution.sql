-- Apply transactionally with the trading runner stopped, before new code starts.
-- RECONCILED is an audited local disposition, never an exchange final status.
ALTER TABLE v2_orders DROP CONSTRAINT v2_orders_status_check;
ALTER TABLE v2_orders ADD CONSTRAINT v2_orders_status_check CHECK (status IN (
    'PREPARED','SUBMITTING','UNKNOWN','ACKNOWLEDGED','FILLED','CANCELLED','REJECTED','RECONCILED'));
ALTER TABLE v2_orders ADD CONSTRAINT v2_orders_reconciled_close_check
    CHECK (status <> 'RECONCILED' OR leg='CLOSE');
