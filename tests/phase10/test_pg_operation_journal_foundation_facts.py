from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "operation_journal/model.py"
SCHEMA = ROOT / "db/postgres_operation_journal_schema.sql"


def test_postgres_owns_operation_stage_without_duplicating_hot_state():
    schema = SCHEMA.read_text()
    assert "CREATE TABLE IF NOT EXISTS trade_operations" in schema
    assert "operation_id UUID PRIMARY KEY" in schema
    assert "version BIGINT NOT NULL" in schema
    assert "trade_operations_recovery_idx" in schema
    assert "CREATE UNIQUE INDEX IF NOT EXISTS trade_operations_request_idx" not in schema
    for forbidden in ("pm:positions", "pm:slot:v1", "pm:desired-protection:v1"):
        assert forbidden not in schema


def test_unknown_and_cas_foundation_are_explicit_and_dormant():
    source = MODEL.read_text()
    assert 'UNKNOWN = "UNKNOWN"' in source
    assert "effect_absence_proven" in source
    assert "version=self.version + 1" in source
    assert "redis" not in source.lower()
    assert "psycopg" not in source.lower()
    assert "position_runtime" not in source
