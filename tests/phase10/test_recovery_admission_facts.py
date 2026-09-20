import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ADMISSION = ROOT / "operation_journal/admission.py"
DOC = ROOT / "docs/v2/P10_D3B_D3D_RECOVERY_ADMISSION_IMPLEMENTATION.md"


def test_admission_has_no_concrete_backend_or_runtime_dependency():
    tree = ast.parse(ADMISSION.read_text())
    imports = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    forbidden = {
        "redis", "psycopg", "requests", "shared", "execution", "strategies",
        "position_runtime", "position_state", "position_protection",
        "position_identity",
    }
    assert not {name.split(".")[0] for name in imports}.intersection(forbidden)


def test_admission_cannot_execute_or_mutate():
    source = ADMISSION.read_text().lower()
    for token in (
        "execute(", "eval(", "fapi", "systemctl", "subprocess", "threading",
        "asyncio", "create_operation", "advance_stage", "compare_and_swap",
    ):
        assert token not in source
    assert 'mutation_ownership_required = "mutation_ownership_required"' in source
    assert "decide_recovery(item.record)" in source
    assert "generation_resolver.revalidate" in source


def test_admission_doc_denies_mutation_permission_and_runtime_wiring():
    source = DOC.read_text()
    assert "dormant admission boundary" in source
    assert "remains prohibited" in source
    assert "No\nresult from this module is standalone permission" in source
    assert "default-off scheduler/runtime activation" in source
