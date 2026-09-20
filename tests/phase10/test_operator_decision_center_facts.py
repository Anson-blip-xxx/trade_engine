from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs/v2/V2_OPERATOR_DECISION_CENTER_PRODUCT_NOTE.md"


def test_decision_center_is_planned_not_claimed_as_implemented():
    source = DOC.read_text()
    assert "PRODUCT REQUIREMENT APPROVED / DESIGN PLANNED / NOT IMPLEMENTED" in source
    assert "adds no notification delivery, HTTP route" in source
    assert "read-only center should ship before any mutation button" in source


def test_telegram_is_attention_channel_not_approval_or_authority():
    source = DOC.read_text()
    assert "High-risk approval must not be a\none-click Telegram action" in source
    assert "Telegram delivery is not business acknowledgement" in source
    assert "durable Web inbox remains authoritative" in source
    assert "never approval grants" in source


def test_web_requires_current_diff_expiry_and_secure_revalidation():
    source = DOC.read_text()
    for phrase in (
        "trigger-time snapshot and current snapshot displayed side by side",
        "automatic\n  fallback countdown",
        "MFA/re-auth",
        "exact\n  `ApprovalBinding`",
        "rebuild-from-current-state",
        "visible new decision version",
    ):
        assert phrase in source


def test_visual_requirement_keeps_accessibility_and_consequence_visible():
    source = DOC.read_text()
    assert "high-density dark trading/observability console" in source
    assert "motion reduced" in source
    assert "no color-only semantics" in source
    assert "must not obscure consequence" in source
