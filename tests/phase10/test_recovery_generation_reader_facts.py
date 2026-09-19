import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
READER = ROOT / "operation_journal/generation_reader.py"


def test_generation_reader_depends_only_on_injected_read_ports():
    tree = ast.parse(READER.read_text())
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
    forbidden_roots = {
        "redis", "psycopg", "requests", "shared", "execution", "strategies",
        "position_runtime", "position_state",
    }
    assert not {name.split(".")[0] for name in imports}.intersection(
        forbidden_roots)
    assert not any(name.endswith("desired_redis") for name in imports)
    assert not any(name.endswith("authority_redis") for name in imports)


def test_generation_reader_has_no_mutation_or_runtime_surface():
    source = READER.read_text()
    lowered = source.lower()
    for forbidden in (
        "compare_and_swap", "create_operation", "advance_stage", "execute(",
        "eval(", "fapi", "systemctl", "threading", "asyncio",
    ):
        assert forbidden not in lowered
    assert "get_slot_authority" in source
    assert "validate_recovery_generation" in source
    assert "AUTHORITY_UNAVAILABLE" in source
    assert "DESIRED_UNAVAILABLE" in source


def test_generation_reader_remains_explicitly_dormant_in_documentation():
    doc = (ROOT / "docs/v2/P10_D3D_RECOVERY_GENERATION_READER_IMPLEMENTATION.md")
    source = doc.read_text()
    assert "dormant read boundary" in source
    assert "does not\nauthorize" in source
    assert "pre-effect revalidation" in source
