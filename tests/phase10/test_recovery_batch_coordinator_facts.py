import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
COORDINATOR = ROOT / "operation_journal/coordinator.py"


def test_batch_coordinator_is_planning_only_and_has_no_io_dependencies():
    tree = ast.parse(COORDINATOR.read_text())
    imports = {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not imports.intersection({
        "redis", "requests", "psycopg", "shared", "execution", "strategies",
        "position_runtime", "position_state", "position_protection",
    })
    source = COORDINATOR.read_text().lower()
    for forbidden in ("execute(", "submit(", "sleep(", "thread", "release_lease"):
        assert forbidden not in source


def test_unknown_and_invariant_fail_closed_dispositions_are_explicit():
    source = COORDINATOR.read_text()
    assert 'JOURNAL_UNKNOWN = "JOURNAL_UNKNOWN"' in source
    assert 'INVARIANT_VIOLATION = "INVARIANT_VIOLATION"' in source
    assert "if claim.records" in source
    assert "duplicate operation_id" in source
    assert "more than one operation per slot" in source
    assert "claimed batch contains terminal operation" in source

