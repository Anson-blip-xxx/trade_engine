import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FENCE = ROOT / "operation_journal/generation_fence.py"


def test_generation_fence_is_pure_and_cannot_mutate_backends():
    tree = ast.parse(FENCE.read_text())
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
        "position_runtime", "position_state",
    })
    source = FENCE.read_text().lower()
    for forbidden in ("execute(", "eval(", "fapi", "compare_and_swap"):
        assert forbidden not in source


def test_open_external_and_protection_gates_are_explicit():
    source = FENCE.read_text()
    assert 'ALLOCATION_REQUIRED = "ALLOCATION_REQUIRED"' in source
    assert 'POLICY_REQUIRED = "POLICY_REQUIRED"' in source
    assert 'QUARANTINED = "QUARANTINED"' in source
    assert 'STALE_PROTECTION = "STALE_PROTECTION"' in source
    assert "desired.last_operation_id != record.operation_id" in source
    assert "fence_passed=True" in source

