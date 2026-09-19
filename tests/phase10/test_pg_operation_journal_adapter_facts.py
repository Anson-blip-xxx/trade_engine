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
    assert "CasCode.UNKNOWN" in source


def test_adapter_does_not_claim_lease_or_recovery_scan_implementation():
    source = ADAPTER.read_text().lower()
    assert "skip locked" not in source
    assert "def acquire" not in source
    assert "def renew" not in source
    assert "def recovery" not in source
