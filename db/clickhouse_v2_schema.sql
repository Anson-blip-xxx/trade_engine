-- Apply to a dedicated V2 database, never the legacy analytics database.
CREATE TABLE v2_trade_events (
    event_id String,
    intent_id String,
    kind String,
    payload String,
    payload_digest FixedString(64)
) ENGINE = ReplacingMergeTree ORDER BY event_id;
-- No time partition: retries of one event must always share a merge partition.
-- All analytics use this view, never sum the physical at-least-once table.
CREATE VIEW v2_trade_events_logical AS
SELECT event_id,intent_id,kind,payload,payload_digest FROM v2_trade_events FINAL;
