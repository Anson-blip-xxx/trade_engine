-- Independent V2 schema; isolated QA only until cutover.
CREATE TABLE v2_decision_evidence (
    evidence_ref TEXT PRIMARY KEY,
    config_digest TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    config JSONB NOT NULL CHECK (jsonb_typeof(config) = 'object'),
    snapshot JSONB NOT NULL CHECK (jsonb_typeof(snapshot) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (evidence_ref, config_digest, strategy_version)
);
CREATE FUNCTION v2_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'immutable business record';
END;
$$;
CREATE TRIGGER v2_evidence_immutable BEFORE UPDATE OR DELETE ON v2_decision_evidence
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();

CREATE TABLE v2_trade_intents (
    intent_id UUID PRIMARY KEY,
    exchange TEXT NOT NULL,
    account_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    product TEXT NOT NULL,
    producer TEXT NOT NULL,
    request_key TEXT NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    payload_digest TEXT NOT NULL CHECK (payload_digest ~ '^[0-9a-f]{64}$'),
    evidence_ref TEXT NOT NULL,
    config_digest TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'RECEIVED' CHECK (status IN (
        'RECEIVED','REJECTED','EXPIRED','EXECUTING','UNKNOWN',
        'CANCELLED','PARTIALLY_FILLED','FILLED')),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    data_revision BIGINT NOT NULL DEFAULT 1 CHECK (data_revision > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (exchange, account_id, environment, product, producer, request_key),
    FOREIGN KEY (evidence_ref, config_digest, strategy_version)
        REFERENCES v2_decision_evidence (evidence_ref, config_digest, strategy_version)
);

CREATE TABLE v2_domain_outbox (
    event_id UUID PRIMARY KEY,
    intent_id UUID NOT NULL REFERENCES v2_trade_intents(intent_id),
    event_type TEXT NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (intent_id, event_type)
);
CREATE FUNCTION v2_guard_intent_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['status','version','data_revision']) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['status','version','data_revision']) THEN
        RAISE EXCEPTION 'immutable intent identity';
    END IF;
    IF NEW.version < OLD.version OR NEW.data_revision < OLD.data_revision THEN
        RAISE EXCEPTION 'intent version cannot move backwards';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER v2_intent_identity BEFORE UPDATE ON v2_trade_intents
FOR EACH ROW EXECUTE FUNCTION v2_guard_intent_identity();
CREATE TRIGGER v2_intent_no_delete BEFORE DELETE ON v2_trade_intents
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
-- Submission requires a committed order-state CAS, never an outbox event alone.

CREATE TABLE v2_episodes (
    episode_id UUID PRIMARY KEY REFERENCES v2_trade_intents(intent_id),
    slot_key TEXT NOT NULL,
    accounting_revision BIGINT NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','SETTLED','ABORTED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE UNIQUE INDEX v2_one_active_episode ON v2_episodes(slot_key) WHERE status='ACTIVE';

CREATE TABLE v2_orders (
    order_id UUID PRIMARY KEY,
    episode_id UUID NOT NULL REFERENCES v2_episodes(episode_id),
    leg TEXT NOT NULL CHECK (leg IN ('OPEN','CLOSE')),
    client_order_id TEXT NOT NULL UNIQUE,
    request_key TEXT NOT NULL,
    quantity NUMERIC(38,18) NOT NULL CHECK (quantity > 0),
    order_type TEXT NOT NULL DEFAULT 'MARKET' CHECK (order_type IN ('MARKET','LIMIT')),
    limit_price NUMERIC(38,18),
    time_in_force TEXT,
    request_evidence JSONB NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(request_evidence)='object'),
    status TEXT NOT NULL DEFAULT 'PREPARED' CHECK (status IN (
        'PREPARED','SUBMITTING','UNKNOWN','ACKNOWLEDGED','FILLED','CANCELLED','REJECTED')),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    exchange_order_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (episode_id, leg, request_key),
    CHECK ((order_type='MARKET' AND limit_price IS NULL AND time_in_force IS NULL)
        OR (order_type='LIMIT' AND limit_price IS NOT NULL AND limit_price>0
            AND time_in_force IS NOT NULL AND time_in_force IN ('GTC','IOC','FOK')))
);
CREATE FUNCTION v2_guard_order_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['status','version','exchange_order_id','updated_at']) IS DISTINCT FROM
       (to_jsonb(OLD) - ARRAY['status','version','exchange_order_id','updated_at']) THEN
        RAISE EXCEPTION 'immutable order identity';
    END IF;
    IF OLD.exchange_order_id IS NOT NULL AND NEW.exchange_order_id IS DISTINCT FROM OLD.exchange_order_id THEN
        RAISE EXCEPTION 'immutable exchange order identity';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'order update requires next version';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER v2_order_identity BEFORE UPDATE ON v2_orders
FOR EACH ROW EXECUTE FUNCTION v2_guard_order_identity();
CREATE TRIGGER v2_order_no_delete BEFORE DELETE ON v2_orders
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE TABLE v2_order_events (
    event_id UUID PRIMARY KEY,
    order_id UUID NOT NULL REFERENCES v2_orders(order_id),
    version BIGINT NOT NULL,
    status TEXT NOT NULL,
    evidence JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (order_id,version)
);
CREATE TRIGGER v2_order_events_immutable BEFORE UPDATE OR DELETE ON v2_order_events
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();

CREATE TABLE v2_order_recovery (
    order_id UUID PRIMARY KEY REFERENCES v2_orders(order_id),
    attempts BIGINT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    error_code TEXT
);
CREATE INDEX v2_recovery_due ON v2_order_recovery(next_attempt_at);

CREATE TABLE v2_fills (
    fill_key TEXT PRIMARY KEY,
    order_id UUID NOT NULL REFERENCES v2_orders(order_id),
    quantity NUMERIC(38,18) NOT NULL CHECK (quantity > 0),
    price NUMERIC(38,18) NOT NULL CHECK (price > 0),
    fee NUMERIC(38,18) NOT NULL,
    fee_currency TEXT NOT NULL,
    occurred_at_ms BIGINT NOT NULL CHECK (occurred_at_ms >= 0),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER v2_fills_immutable BEFORE UPDATE OR DELETE ON v2_fills
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE INDEX v2_fills_order ON v2_fills(order_id);
CREATE TABLE v2_fill_observations (
    fill_key TEXT NOT NULL REFERENCES v2_fills(fill_key),
    evidence_digest TEXT NOT NULL CHECK (evidence_digest ~ '^[0-9a-f]{64}$'),
    evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence)='object'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (fill_key,evidence_digest)
);
CREATE TRIGGER v2_fill_observations_immutable BEFORE UPDATE OR DELETE ON v2_fill_observations
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE TABLE v2_cash_adjustments (
    adjustment_key TEXT PRIMARY KEY,
    episode_id UUID NOT NULL REFERENCES v2_episodes(episode_id),
    amount NUMERIC(38,18) NOT NULL,
    currency TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('FUNDING','CORRECTION')),
    occurred_at_ms BIGINT NOT NULL CHECK (occurred_at_ms >= 0),
    evidence JSONB NOT NULL
);
CREATE TRIGGER v2_cash_immutable BEFORE UPDATE OR DELETE ON v2_cash_adjustments
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE INDEX v2_cash_episode ON v2_cash_adjustments(episode_id);

-- Unallocated external income must survive process restarts independently of PnL.
CREATE TABLE v2_income_imports (
    run_id UUID PRIMARY KEY,
    scope JSONB NOT NULL CHECK (jsonb_typeof(scope)='object'),
    start_ms BIGINT NOT NULL CHECK (start_ms>=0), end_ms BIGINT NOT NULL CHECK (end_ms>=start_ms),
    status TEXT NOT NULL CHECK (status IN ('RUNNING','FETCHED','PARTIAL','FAILED')),
    pages BIGINT NOT NULL DEFAULT 0 CHECK (pages>=0), row_count BIGINT NOT NULL DEFAULT 0 CHECK (row_count>=0),
    error_code TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE FUNCTION v2_guard_income_import() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status <> 'RUNNING' OR
       (NEW.run_id,NEW.scope,NEW.start_ms,NEW.end_ms,NEW.started_at) IS DISTINCT FROM
       (OLD.run_id,OLD.scope,OLD.start_ms,OLD.end_ms,OLD.started_at) OR
       NEW.pages < OLD.pages OR NEW.row_count < OLD.row_count THEN
        RAISE EXCEPTION 'immutable import identity or terminal result';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER v2_income_import_guard BEFORE UPDATE ON v2_income_imports
FOR EACH ROW EXECUTE FUNCTION v2_guard_income_import();
CREATE TRIGGER v2_income_import_no_delete BEFORE DELETE ON v2_income_imports
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE TABLE v2_exchange_income (
    income_id UUID PRIMARY KEY,
    exchange TEXT NOT NULL, account_id TEXT NOT NULL, environment TEXT NOT NULL, product TEXT NOT NULL,
    income_type TEXT NOT NULL, source_id TEXT NOT NULL, symbol TEXT NOT NULL,
    amount NUMERIC(38,18) NOT NULL, currency TEXT NOT NULL,
    occurred_at_ms BIGINT NOT NULL CHECK (occurred_at_ms >= 0),
    evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence)='object'),
    received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE(exchange,account_id,environment,product,income_type,source_id)
);
CREATE TRIGGER v2_income_immutable BEFORE UPDATE OR DELETE ON v2_exchange_income
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE INDEX v2_income_scope_time ON v2_exchange_income(exchange,account_id,environment,product,occurred_at_ms);
CREATE TABLE v2_income_allocations (
    income_id UUID PRIMARY KEY REFERENCES v2_exchange_income(income_id),
    episode_id UUID NOT NULL REFERENCES v2_episodes(episode_id),
    adjustment_key TEXT NOT NULL UNIQUE REFERENCES v2_cash_adjustments(adjustment_key),
    evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence)='object')
);
CREATE TRIGGER v2_income_allocation_immutable BEFORE UPDATE OR DELETE ON v2_income_allocations
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE FUNCTION v2_guard_income_allocation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM v2_exchange_income i JOIN v2_cash_adjustments c ON c.adjustment_key=NEW.adjustment_key
        JOIN v2_trade_intents t ON t.intent_id=c.episode_id
        WHERE i.income_id=NEW.income_id AND c.episode_id=NEW.episode_id AND i.income_type='FUNDING_FEE'
        AND c.kind='FUNDING' AND c.amount=i.amount AND c.currency=i.currency AND c.occurred_at_ms=i.occurred_at_ms
        AND (i.exchange,i.account_id,i.environment,i.product,i.symbol)=
            (t.exchange,t.account_id,t.environment,t.product,t.payload->>'symbol')
    ) THEN
        RAISE EXCEPTION 'income allocation must match original financial fact and owner';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER v2_income_allocation_owner BEFORE INSERT ON v2_income_allocations
FOR EACH ROW EXECUTE FUNCTION v2_guard_income_allocation();

-- Historical valuation, not an assertion of a currency exchange transaction.
CREATE TABLE v2_fx_valuations (
    episode_id UUID NOT NULL REFERENCES v2_episodes(episode_id),
    request_key TEXT NOT NULL,
    fill_key TEXT REFERENCES v2_fills(fill_key),
    adjustment_key TEXT REFERENCES v2_cash_adjustments(adjustment_key),
    fact_key TEXT GENERATED ALWAYS AS (COALESCE(fill_key,adjustment_key)) STORED,
    fact_kind TEXT GENERATED ALWAYS AS (CASE WHEN fill_key IS NULL THEN 'CASH' ELSE 'FEE' END) STORED,
    source_currency TEXT NOT NULL,
    target_currency TEXT NOT NULL,
    version BIGINT NOT NULL CHECK (version > 0),
    rate NUMERIC(38,18) NOT NULL CHECK (rate > 0),
    quote_at_ms BIGINT NOT NULL CHECK (quote_at_ms >= 0),
    evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence)='object'),
    CHECK ((fill_key IS NULL) <> (adjustment_key IS NULL)),
    CHECK (source_currency <> target_currency),
    PRIMARY KEY (episode_id,request_key),
    UNIQUE (fact_kind,fact_key,target_currency,version)
);
CREATE TRIGGER v2_fx_immutable BEFORE UPDATE OR DELETE ON v2_fx_valuations
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE FUNCTION v2_guard_valuation_owner() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE owner_id UUID; original_currency TEXT;
BEGIN
    IF NEW.fill_key IS NOT NULL THEN
        SELECT o.episode_id,f.fee_currency INTO owner_id,original_currency
        FROM v2_fills f JOIN v2_orders o USING(order_id) WHERE f.fill_key=NEW.fill_key;
    ELSE
        SELECT episode_id,currency INTO owner_id,original_currency
        FROM v2_cash_adjustments WHERE adjustment_key=NEW.adjustment_key;
    END IF;
    IF owner_id IS DISTINCT FROM NEW.episode_id OR original_currency IS DISTINCT FROM NEW.source_currency THEN
        RAISE EXCEPTION 'valuation must belong to original fact owner and currency';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER v2_fx_owner BEFORE INSERT ON v2_fx_valuations
FOR EACH ROW EXECUTE FUNCTION v2_guard_valuation_owner();

CREATE TABLE v2_consumer_receipts (
    consumer TEXT NOT NULL,
    event_id UUID NOT NULL REFERENCES v2_domain_outbox(event_id),
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (consumer,event_id)
);
CREATE TABLE v2_delivery_attempts (
    consumer TEXT NOT NULL,
    event_id UUID NOT NULL REFERENCES v2_domain_outbox(event_id),
    attempts BIGINT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    lease_until TIMESTAMPTZ,
    lease_token UUID,
    error_code TEXT,
    PRIMARY KEY (consumer,event_id)
);
CREATE INDEX v2_delivery_due ON v2_delivery_attempts(consumer,next_attempt_at);
CREATE TRIGGER v2_outbox_immutable BEFORE UPDATE OR DELETE ON v2_domain_outbox
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();

CREATE TABLE v2_settlements (
    episode_id UUID NOT NULL REFERENCES v2_episodes(episode_id),
    revision BIGINT NOT NULL,
    currency TEXT NOT NULL,
    evidence JSONB NOT NULL,
    settled_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (episode_id, revision)
);
CREATE TRIGGER v2_settlement_immutable BEFORE UPDATE OR DELETE ON v2_settlements
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();

-- Non-ledger strategy state. Every write is versioned, audited and idempotent.
-- Financial mutations spanning these rows and orders require one transaction.
CREATE TABLE v2_business_state (
    state_id UUID PRIMARY KEY,
    scope JSONB NOT NULL CHECK (jsonb_typeof(scope)='object'),
    version BIGINT NOT NULL CHECK (version >= 0),
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload)='object'),
    deleted BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE TABLE v2_state_history (
    event_id UUID PRIMARY KEY,
    state_id UUID NOT NULL REFERENCES v2_business_state(state_id),
    request_key TEXT NOT NULL,
    expected_version BIGINT NOT NULL,
    version BIGINT NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload)='object'),
    deleted BOOLEAN NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (state_id,version),
    UNIQUE (state_id,request_key)
);
CREATE TRIGGER v2_state_history_immutable BEFORE UPDATE OR DELETE ON v2_state_history
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
ALTER TABLE v2_business_state ADD CONSTRAINT v2_state_current_has_history
FOREIGN KEY (state_id,version) REFERENCES v2_state_history(state_id,version)
DEFERRABLE INITIALLY DEFERRED;
CREATE FUNCTION v2_guard_state_version() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state_id IS DISTINCT FROM OLD.state_id OR NEW.scope IS DISTINCT FROM OLD.scope THEN
        RAISE EXCEPTION 'immutable business state identity';
    END IF;
    IF NEW.version <> OLD.version + 1 THEN
        RAISE EXCEPTION 'business state requires next version';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER v2_state_version BEFORE UPDATE ON v2_business_state
FOR EACH ROW EXECUTE FUNCTION v2_guard_state_version();
CREATE TRIGGER v2_state_no_delete BEFORE DELETE ON v2_business_state
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();

CREATE TABLE v2_inbound_signals (
    signal_id UUID PRIMARY KEY,
    source TEXT NOT NULL,
    environment TEXT NOT NULL,
    request_key TEXT NOT NULL,
    snapshot JSONB NOT NULL CHECK (jsonb_typeof(snapshot)='object'),
    received_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (source,environment,request_key)
);
CREATE TRIGGER v2_signals_immutable BEFORE UPDATE OR DELETE ON v2_inbound_signals
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE TABLE v2_signal_receipts (
    consumer TEXT NOT NULL,
    signal_id UUID NOT NULL REFERENCES v2_inbound_signals(signal_id),
    outcome TEXT NOT NULL CHECK (outcome IN ('INTENT','IGNORED','EXPIRED')),
    intent_id UUID REFERENCES v2_trade_intents(intent_id),
    reason TEXT NOT NULL,
    completed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (consumer,signal_id),
    CHECK ((outcome='INTENT' AND intent_id IS NOT NULL) OR
           (outcome<>'INTENT' AND intent_id IS NULL))
);
CREATE TRIGGER v2_signal_receipts_immutable BEFORE UPDATE OR DELETE ON v2_signal_receipts
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
ALTER TABLE v2_decision_evidence ADD COLUMN signal_id UUID
GENERATED ALWAYS AS ((snapshot->>'signal_id')::uuid) STORED
REFERENCES v2_inbound_signals(signal_id);
ALTER TABLE v2_trade_intents ADD COLUMN signal_id UUID REFERENCES v2_inbound_signals(signal_id);
CREATE UNIQUE INDEX v2_one_intent_per_signal_consumer ON v2_trade_intents
(exchange,account_id,environment,product,producer,signal_id) WHERE signal_id IS NOT NULL;

ALTER TABLE v2_decision_evidence ADD UNIQUE (evidence_ref,signal_id);
CREATE TABLE v2_strategy_decisions (
    decision_id UUID PRIMARY KEY,
    consumer TEXT NOT NULL,
    signal_id UUID NOT NULL REFERENCES v2_inbound_signals(signal_id),
    evidence_ref TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('OPEN','IGNORED','EXPIRED')),
    side TEXT,
    quantity NUMERIC(38,18),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    FOREIGN KEY (evidence_ref,signal_id) REFERENCES v2_decision_evidence(evidence_ref,signal_id),
    UNIQUE (consumer,signal_id),
    CHECK ((action='OPEN' AND side IS NOT NULL AND side IN ('BUY','SELL') AND quantity IS NOT NULL AND quantity>0)
        OR (action<>'OPEN' AND side IS NULL AND quantity IS NULL))
);
CREATE TRIGGER v2_strategy_decisions_immutable BEFORE UPDATE OR DELETE ON v2_strategy_decisions
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();

-- Scheduling is not a signal receipt: INTENT can still be waiting for capacity.
CREATE TABLE v2_strategy_tasks (
    consumer TEXT NOT NULL,
    signal_id UUID NOT NULL REFERENCES v2_inbound_signals(signal_id),
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    lease_token UUID,
    lease_until TIMESTAMPTZ,
    attempts BIGINT NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    error_code TEXT,
    completed_at TIMESTAMPTZ,
    PRIMARY KEY (consumer,signal_id),
    CHECK ((lease_token IS NULL) = (lease_until IS NULL))
);
CREATE INDEX v2_strategy_tasks_due ON v2_strategy_tasks(consumer,next_attempt_at,signal_id)
WHERE completed_at IS NULL;

-- Immutable producer confirmation, not a market tick history store.
CREATE TABLE v2_producer_batches (
    source TEXT NOT NULL CHECK (source IN ('s0','s2','s3')),
    environment TEXT NOT NULL CHECK (environment IN ('SANDBOX','LIVE')),
    frame_id TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    observed_at_ms BIGINT NOT NULL CHECK (observed_at_ms >= 0),
    signal_ids JSONB NOT NULL CHECK (jsonb_typeof(signal_ids)='array'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (source,environment,frame_id)
);
CREATE TRIGGER v2_producer_batch_immutable BEFORE UPDATE OR DELETE ON v2_producer_batches
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
