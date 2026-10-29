-- Only columns needed to reject stale settlement revisions; no business writes.
GRANT SELECT (episode_id,accounting_revision,status) ON trade_v2.v2_episodes TO tradev2accounts;
