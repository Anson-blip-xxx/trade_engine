-- Isolated public-market research checkpoints. No trading-state dependencies.
CREATE TABLE IF NOT EXISTS v2_mover_flow_evidence (
 day date NOT NULL,
 symbol text NOT NULL CHECK (char_length(symbol) BETWEEN 5 AND 44),
 method_version text NOT NULL DEFAULT 'mover-flow-public-v1'
   CHECK (method_version='mover-flow-public-v1'),
 payload jsonb NOT NULL CHECK (
   jsonb_typeof(payload)='object' AND octet_length(payload::text)<=262144
 ),
 input_digest text NOT NULL CHECK (input_digest ~ '^[0-9a-f]{64}$'),
 attempts integer NOT NULL DEFAULT 1 CHECK (attempts BETWEEN 1 AND 3),
 complete boolean NOT NULL,
 updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(day,symbol,method_version)
);
-- The primary key's leading day column supports bounded retention scans.
-- Checkpoints expire after 30 days; immutable report features live elsewhere.
