-- Apply after the research migration using the existing tradev2movers role.
-- No credentials, new role, or trading-table privileges are needed.
GRANT SELECT,INSERT,UPDATE,DELETE
 ON trade_v2.v2_mover_flow_evidence TO tradev2movers;
