import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADAPTER = ROOT / "operation_journal/postgres.py"


def test_adapter_is_injected_and_not_runtime_wired():
    source = ADAPTER.read_text()
    assert "connection_factory" in source
    assert "import psycopg" not in source.lower()
    assert "POSTGRES_DSN" not in source
    assert "os.environ" not in source
    runtime_importers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in {"tests", "operation_journal"}:
            continue
        tree = ast.parse(path.read_text())
        names = [
            node.module for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        ] + [
            alias.name for node in ast.walk(tree)
            if isinstance(node, ast.Import) for alias in node.names
        ]
        if any(name == "operation_journal" or name.startswith("operation_journal.")
               for name in names):
            runtime_importers.append(str(relative))
    assert runtime_importers == []


def test_sql_cas_fences_version_and_owner_and_returns_authoritative_row():
    source = ADAPTER.read_text()
    assert "AND version = %(expected_version)s" in source
    assert "owner_token IS NOT DISTINCT FROM %(expected_owner_token)s" in source
    assert "RETURNING operation_id::text" in source
    assert "CasCode.STALE_VERSION" in source
    assert "CasCode.OWNER_MISMATCH" in source
    assert "CasCode.LEASE_EXPIRED" in source
    assert "CasCode.UNKNOWN" in source


def test_lease_uses_database_clock_and_recovery_scan_is_still_deferred():
    source = ADAPTER.read_text().lower()
    assert "def claim_lease" in source
    assert "def renew_lease" in source
    assert "def release_lease" in source
    assert "lease_expires_at > clock_timestamp()" in source
    assert "lease_expires_at <= clock_timestamp()" in source
    assert "version = version + 1" in source
    assert "skip locked" not in source
    assert "def recovery" not in source
