"""R4B migrated verification composition guards."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "position_protection/migrated_verification.py"


def test_exact_declaration_gates_single_coordinator_step():
    source = SOURCE.read_text()
    gate = source.index("if not declaration.may_verify:")
    step = source.index("self._coordinator.step(")
    assert gate < step
    assert source.count("self._coordinator.step(") == 1
    assert "desired.exchange_position_key" in source


def test_composition_is_dormant_and_non_blocking():
    source = SOURCE.read_text().lower()
    for forbidden in ("while ", "sleep(", "except ", "requests", "redis", "position_runtime"):
        assert forbidden not in source
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_protection"):
            continue
        if "MigratedProtectionVerificationService" in path.read_text():
            callers.append(str(relative))
    assert callers == []
