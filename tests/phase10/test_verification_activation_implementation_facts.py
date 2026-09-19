"""D2C-RUNTIME-GATE architecture facts."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "position_protection/verification_activation.py"
MODEL = ROOT / "docs/v2/P10_D2C_RUNTIME_ACTIVATION_GATE_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def test_gate_is_default_off_and_requires_every_activation_contract():
    source = SOURCE.read_text()
    assert "enabled: bool = False" in source
    for token in (
        "ENDPOINTS_UNAPPROVED",
        "DURABLE_SCHEDULER_UNAPPROVED",
        "OPERATOR_OUTCOME_SINK_UNAPPROVED",
        "FEATURE_DISABLED",
    ):
        assert token in source


def test_gate_has_no_runtime_or_io_dependency():
    source = SOURCE.read_text()
    for forbidden in (
        "os.environ",
        "requests",
        "httpx",
        "redis",
        "threading",
        "position_runtime",
        "shared.position_manager",
    ):
        assert forbidden not in source


def test_gate_remains_dormant_and_docs_preserve_product_decision():
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_protection"):
            continue
        if "assess_verification_activation" in path.read_text():
            callers.append(str(relative))
    assert callers == []
    assert "product/operations selection" in MODEL.read_text()
    assert "D2C Runtime Activation Gate | **IMPLEMENTED / CLOSED (DORMANT)**" in BACKLOG.read_text()
