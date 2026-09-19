import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DECISION = ROOT / "operation_journal/recovery.py"


def test_decision_engine_is_pure_and_has_no_runtime_dependencies():
    tree = ast.parse(DECISION.read_text())
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
    source = DECISION.read_text().lower()
    for forbidden in ("sleep(", "thread", "socket", "fapi", "binance"):
        assert forbidden not in source


def test_ambiguity_and_special_operation_safety_are_explicit():
    source = DECISION.read_text()
    assert "OperationStage.SUBMITTING, OperationStage.UNKNOWN" in source
    assert "ExchangeAccess.QUERY_ONLY" in source
    assert "absence=True" in source
    assert "OperationType.GHOST_FINALIZE" in source
    assert "OperationType.EXTERNAL_RECONCILE" in source
    assert "RecoveryDirective.CREATE_COMPENSATION_OPERATION" in source
    assert "automatic=False" in source


def test_pending_work_shape_is_guarded_in_model_and_schema():
    model = (ROOT / "operation_journal/model.py").read_text()
    schema = (ROOT / "db/postgres_operation_journal_schema.sql").read_text()
    assert "requires pending requirements" in model
    assert "pending requirements must be unique" in model
    assert "COMPLETED cannot retain pending requirements" in model
    assert "jsonb_typeof(pending_requirements) = 'array'" in schema
    assert "jsonb_typeof(input) = 'object'" in schema

