import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "operator_decision/model.py"
SCHEMA = ROOT / "db/postgres_operator_decision_schema.sql"
DOC = ROOT / "docs/v2/V2_OPERATOR_DECISION_FOUNDATION_IMPLEMENTATION.md"


def test_schema_defines_separate_inbox_and_retryable_outbox():
    source = SCHEMA.read_text()
    assert "CREATE TABLE IF NOT EXISTS operator_decisions" in source
    assert "CREATE TABLE IF NOT EXISTS operator_notification_outbox" in source
    assert "REFERENCES operator_decisions(decision_id)" in source
    assert "dedupe_key TEXT NOT NULL UNIQUE" in source
    assert "operator_notification_due_idx" in source
    assert "operator_notification_expired_claim_idx" in source
    assert "WHERE status IN ('PENDING','RETRY_WAIT')" in source
    assert "(owner_token IS NULL) = (lease_expires_at IS NULL)" in source
    assert "(status = 'PENDING' AND attempt_count = 0)" in source
    assert "status <> 'CLAIMED' OR lease_expires_at > updated_at" in source
    assert "status <> 'DEAD_LETTER' OR last_error IS NOT NULL" in source


def test_model_is_backend_neutral_and_cannot_deliver_notifications():
    tree = ast.parse(MODEL.read_text())
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
        "psycopg", "redis", "requests", "httpx", "shared", "execution",
        "position_runtime", "subprocess", "threading", "asyncio",
    }
    assert not {name.split(".")[0] for name in imports}.intersection(forbidden)
    lowered = MODEL.read_text().lower()
    for token in ("requests.post", "sendmessage", "execute(", "systemctl"):
        assert token not in lowered


def test_existing_runtime_does_not_import_decision_center():
    importers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in {"tests", "operator_decision"}:
            continue
        tree = ast.parse(path.read_text())
        names = [
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        ] + [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        if any(name == "operator_decision" or
               name.startswith("operator_decision.") for name in names):
            importers.append(str(relative))
    assert importers == []


def test_foundation_is_explicitly_unapplied_and_dormant():
    source = DOC.read_text()
    assert "standalone schema was\n> not applied to any database" in source
    assert "There is no DSN" in source
    assert "Web inbox is not trading authority" in source
    assert "Telegram delivery is not business\nacknowledgement" in source
