import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / "operation_journal/delayed_policy.py"
DOC = ROOT / "docs/v2/V2_AUTONOMOUS_DELAYED_EVENT_POLICY_DECISION.md"


def test_delayed_policy_is_pure_and_backend_neutral():
    tree = ast.parse(POLICY.read_text())
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
        "position_runtime", "position_protection", "position_state",
        "subprocess", "threading", "asyncio",
    }
    assert not {name.split(".")[0] for name in imports}.intersection(forbidden)
    source = POLICY.read_text().lower()
    for token in ("execute(", "eval(", "fapi", "systemctl", "sleep("):
        assert token not in source


def test_policy_encodes_safe_defaults_and_expiring_context_binding():
    source = POLICY.read_text()
    assert "automatic_emergency_close: bool = False" in source
    assert 'CANCEL_REQUEST = "CANCEL_REQUEST"' in source
    assert 'QUERY_AND_RECONCILE = "QUERY_AND_RECONCILE"' in source
    assert 'REBUILD_DECISION = "REBUILD_DECISION"' in source
    assert "event.context_digest != current_digest" in source
    assert "current >= approval.expires_at" in source
    assert "approval.binding != expected" in source
    assert "requires_mutation_ownership=True" in source


def test_approved_doc_denies_hidden_deadlines_and_side_effect_permission():
    source = DOC.read_text()
    assert "PRODUCT DEFAULT APPROVED / DORMANT CONTRACT IMPLEMENTED" in source
    assert "no hidden production durations" in source
    assert "never blindly retried" in source
    assert "Automatic market close is disabled by default" in source
    assert "cannot be\n   rebound to a later episode" in source
    assert "not side-effect permission" in source
