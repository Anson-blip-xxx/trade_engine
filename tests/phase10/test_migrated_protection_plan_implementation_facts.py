"""R4B migrated-protection planner guards."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "position_protection/migrated_plan.py"


def test_planner_is_pure_and_never_marks_active():
    source = SOURCE.read_text()
    for forbidden in (
        "redis",
        "requests",
        "psycopg",
        "position_runtime",
        'status="active"',
    ):
        assert forbidden not in source.lower()
    assert "DesiredProtectionRecord.initial_pending(" in source
    assert 'status="PENDING"' in source
    assert "handoff.applied" in source


def test_planner_is_dormant():
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_protection"):
            continue
        if "plan_migrated_protection" in path.read_text():
            callers.append(str(relative))
    assert callers == []
