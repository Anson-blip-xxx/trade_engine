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
    status TEXT NOT NULL DEFAULT 'PREPARED' CHECK (status IN (
        'PREPARED','SUBMITTING','UNKNOWN','ACKNOWLEDGED','FILLED','CANCELLED','REJECTED')),
    version BIGINT NOT NULL DEFAULT 1 CHECK (version > 0),
    exchange_order_id TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (episode_id, leg, request_key)
);
CREATE UNIQUE INDEX v2_one_open_order ON v2_orders(episode_id) WHERE leg='OPEN';
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

CREATE TABLE v2_fills (
    fill_key TEXT PRIMARY KEY,
    order_id UUID NOT NULL REFERENCES v2_orders(order_id),
    quantity NUMERIC(38,18) NOT NULL CHECK (quantity > 0),
    price NUMERIC(38,18) NOT NULL CHECK (price > 0),
    fee NUMERIC(38,18) NOT NULL CHECK (fee >= 0),
    fee_currency TEXT NOT NULL,
    occurred_at_ms BIGINT NOT NULL CHECK (occurred_at_ms >= 0),
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER v2_fills_immutable BEFORE UPDATE OR DELETE ON v2_fills
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
CREATE INDEX v2_fills_order ON v2_fills(order_id);
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
