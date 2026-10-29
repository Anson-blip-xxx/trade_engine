-- Narrow administrator-owned, security-barrier projections. No raw JSON,
-- account names, API credentials, ledger writes, or trading table grants.
CREATE OR REPLACE VIEW trade_v2_research.actual_exit_intents WITH (security_barrier=true) AS
 SELECT i.intent_id, i.created_at,
 md5(jsonb_build_array(i.exchange,i.account_id,i.environment,i.product)::text) scope_key,
 i.environment, i.producer, i.payload->>'symbol' symbol, i.payload->>'side' side,
 e.snapshot->>'source' source, e.evidence_ref, e.config_digest, e.strategy_version,
 e.snapshot->>'decided_at' decided_ms,
 e.snapshot->'features'->'evaluation'->'sizing'->>'stop_price' stop_price,
 EXISTS(SELECT 1 FROM trade_v2.v2_orders o WHERE o.episode_id=i.intent_id
   AND o.request_evidence->>'origin'='APPROVED_TESTNET_MAINTENANCE') maintenance,
 (EXISTS(SELECT 1 FROM trade_v2.v2_orders o WHERE o.episode_id=i.intent_id AND o.leg='OPEN')
  AND NOT EXISTS(SELECT 1 FROM trade_v2.v2_orders o WHERE o.episode_id=i.intent_id AND o.leg='OPEN'
    AND o.status NOT IN ('FILLED','CANCELLED','REJECTED'))) opening_terminal,
 e.created_at evidence_created_at,
 (SELECT max(o.updated_at) FROM trade_v2.v2_orders o WHERE o.episode_id=i.intent_id AND o.leg='OPEN') opening_observed_at,
 jsonb_path_exists(e.snapshot,'$.features.context.signal_fusion.peers[*] ? (@.source == "tv_bridge")') tv_peer_present
 FROM trade_v2.v2_trade_intents i JOIN trade_v2.v2_decision_evidence e USING(evidence_ref)
 WHERE i.environment='SANDBOX' AND i.exchange='BINANCE' AND i.product='FUTURES'
   AND i.producer IN ('s6','s8') AND e.snapshot->>'source' IN ('s3','tv_bridge')
   AND EXISTS(SELECT 1 FROM trade_v2.v2_orders o JOIN trade_v2.v2_fills f USING(order_id)
      WHERE o.episode_id=i.intent_id AND o.leg='OPEN');

CREATE VIEW trade_v2_research.actual_exit_fills WITH (security_barrier=true) AS
 SELECT i.intent_id, f.fill_key identity, f.occurred_at_ms t, o.leg,
 f.quantity::text quantity, f.price::text price, f.fee::text fee,
 f.fee_currency, f.created_at
 FROM trade_v2_research.actual_exit_intents i JOIN trade_v2.v2_orders o ON o.episode_id=i.intent_id
 JOIN trade_v2.v2_fills f USING(order_id);

CREATE VIEW trade_v2_research.actual_exit_settlements WITH (security_barrier=true) AS
 SELECT i.intent_id,s.revision,s.settled_at,s.evidence->>'net_pnl' net_pnl
 FROM trade_v2_research.actual_exit_intents i JOIN trade_v2.v2_settlements s ON s.episode_id=i.intent_id
 WHERE s.currency='USDT';

GRANT CONNECT ON DATABASE trade_v2_testnet TO tradev2exitfacts;
GRANT USAGE ON SCHEMA trade_v2_research TO tradev2exitfacts;
GRANT SELECT ON trade_v2_research.actual_exit_intents,trade_v2_research.actual_exit_fills,
 trade_v2_research.actual_exit_settlements TO tradev2exitfacts;
GRANT SELECT,INSERT ON trade_v2_research.actual_exit_studies TO tradev2exitfacts;
GRANT SELECT,INSERT,UPDATE,DELETE ON trade_v2_research.actual_exit_jobs TO tradev2exitfacts;
CREATE VIEW trade_v2_research.actual_exit_control_samples WITH (security_barrier=true) AS
 SELECT symbol,start_ms,content_digest,payload_json FROM trade_v2_research.exit_samples
 WHERE mode='FORWARD' AND status='SIMULATED';
GRANT SELECT ON trade_v2_research.actual_exit_control_samples TO tradev2exitfacts;
GRANT SELECT,INSERT,DELETE ON trade_v2_research.exit_funnel_audits TO tradev2exitfacts;
