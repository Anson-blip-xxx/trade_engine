-- Apply research_exit_schema.sql in this schema first, owned by postgres.
GRANT CONNECT ON DATABASE trade_v2_testnet TO tradev2research;
GRANT USAGE ON SCHEMA trade_v2_research TO tradev2research;
GRANT SELECT,INSERT ON trade_v2_research.exit_studies TO tradev2research;
GRANT SELECT,INSERT,UPDATE ON trade_v2_research.exit_cursors TO tradev2research;
GRANT SELECT,INSERT,DELETE ON trade_v2_research.exit_samples TO tradev2research;
-- No trading tables, account credentials, PG functions, or schema ownership.
