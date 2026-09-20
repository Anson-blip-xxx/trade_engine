"""Keep the new authoritative core independent of file/legacy storage."""

import ast
from pathlib import Path


def test_data_core_has_no_file_storage_or_legacy_runtime_imports():
    root = Path(__file__).resolve().parents[2] / "v2_core"
    forbidden = {
        "sqlite3",
        "pickle",
        "shelve",
        "pathlib",
        "shared",
        "strategies",
        "services",
        "position_runtime",
    }
    for source in root.glob("*.py"):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(
                    alias.name.split(".")[0] not in forbidden for alias in node.names
                ), source
            elif isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] not in forbidden, source
            elif isinstance(node, ast.Call):
                name = (
                    node.func.id
                    if isinstance(node.func, ast.Name)
                    else getattr(node.func, "attr", "")
                )
                assert name not in {
                    "open",
                    "write_text",
                    "write_bytes",
                    "read_text",
                    "read_bytes",
                    "rename",
                    "replace_file",
                }, source
