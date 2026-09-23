-- Add immutable S6/S8 settlement outcomes and their T60 observations.
-- Apply to a V2 database that predates these greenfield-schema objects.
-- The deployer owns BEGIN/COMMIT and must select the trade_v2 search_path.
SET LOCAL lock_timeout TO '5s';

CREATE TABLE IF NOT EXISTS v2_directional_outcomes (
    episode_id UUID PRIMARY KEY REFERENCES v2_episodes(episode_id),
    exchange TEXT NOT NULL,
    account_id TEXT NOT NULL,
    environment TEXT NOT NULL,
    product TEXT NOT NULL,
    producer TEXT NOT NULL CHECK (producer IN ('s6','s8')),
    symbol TEXT NOT NULL,
    event_type TEXT NOT NULL,
    closed_at_ms BIGINT NOT NULL CHECK (closed_at_ms >= 0),
    settlement_revision BIGINT NOT NULL CHECK (settlement_revision >= 0),
    net_pnl NUMERIC(38,18) NOT NULL,
    return_pct NUMERIC(38,18) NOT NULL,
    quality_score NUMERIC(38,18) NOT NULL CHECK (quality_score BETWEEN 0 AND 100),
    closing_price NUMERIC(38,18) NOT NULL CHECK (closing_price > 0),
    evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence)='object'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX IF NOT EXISTS v2_directional_outcomes_rollup ON v2_directional_outcomes(
    exchange,account_id,environment,product,producer,symbol,event_type,closed_at_ms
);
DROP TRIGGER IF EXISTS v2_directional_outcomes_immutable ON v2_directional_outcomes;
CREATE TRIGGER v2_directional_outcomes_immutable
BEFORE UPDATE OR DELETE ON v2_directional_outcomes
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();

CREATE TABLE IF NOT EXISTS v2_directional_followups (
    episode_id UUID NOT NULL REFERENCES v2_directional_outcomes(episode_id),
    horizon_minutes BIGINT NOT NULL CHECK (horizon_minutes=60),
    observed_at_ms BIGINT NOT NULL CHECK (observed_at_ms >= 0),
    price NUMERIC(38,18) NOT NULL CHECK (price > 0),
    post_close_return_pct NUMERIC(38,18) NOT NULL,
    evidence JSONB NOT NULL CHECK (jsonb_typeof(evidence)='object'),
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (episode_id,horizon_minutes)
);
DROP TRIGGER IF EXISTS v2_directional_followups_immutable ON v2_directional_followups;
CREATE TRIGGER v2_directional_followups_immutable
BEFORE UPDATE OR DELETE ON v2_directional_followups
FOR EACH ROW EXECUTE FUNCTION v2_reject_mutation();
