"""R4B migrated declaration guards."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "position_protection/migrated_declaration.py"


def test_only_exact_durable_ack_permits_verification():
    source = SOURCE.read_text()
    assert "write.record == plan.desired" in source
    assert "write.applied" in source
    assert "self._port.declare(plan.desired)" in source
    assert source.count("self._port.declare(") == 1


def test_service_is_dormant_and_has_no_retry_or_io():
    source = SOURCE.read_text().lower()
    for forbidden in ("while ", "sleep(", "except ", "requests", "redis", "position_runtime"):
        assert forbidden not in source
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_protection"):
            continue
        if "MigratedProtectionDeclarationService" in path.read_text():
            callers.append(str(relative))
    assert callers == []
