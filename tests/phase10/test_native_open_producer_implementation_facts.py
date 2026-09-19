"""R4A architecture guards for native active-open producer wiring."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "docs/v2/P10_R4A_NATIVE_OPEN_PRODUCER_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def test_handoff_is_one_three_key_atomic_script():
    source = (ROOT / "position_protection/handoff_redis.py").read_text()
    assert "NATIVE_OPEN_HANDOFF_LUA" in source
    assert "NATIVE_OPEN_HANDOFF_LUA,\n                3," in source
    assert source.count("redis.call('SET', KEYS[") == 3
    for token in (
        "same(current_authority, ARGV[1])",
        "same(current_projection, ARGV[2])",
        "same(current_desired, ARGV[3])",
    ):
        assert token in source


def test_native_producer_enqueues_only_acked_v3_identity():
    source = (ROOT / "shared/position_manager.py").read_text()
    body = source.split("def _algo_enqueue_native_open(", 1)[1].split(
        "def _algo_enqueue(", 1
    )[0]
    assert "if not handoff.applied:" in body
    assert "return handoff" in body
    assert "episode_id=authority.episode_id" in body
    assert "protection_generation=desired.protection_generation" in body
    assert "desired_revision=desired.revision" in body
    assert "projection_revision=projection.state_revision" in body


def test_active_open_requires_explicit_optional_principal():
    source = (ROOT / "strategies/shared_executor.py").read_text()
    assert "_ACCOUNT_PRINCIPAL_ID = ''" in source
    assert "k == 'ACCOUNT_PRINCIPAL_ID'" in source
    assert "if _ACCOUNT_PRINCIPAL_ID:" in source
    configured = source.split("if _ACCOUNT_PRINCIPAL_ID:", 1)[1].split("else:", 1)[0]
    assert "_algo_enqueue_native_open(" in configured
    assert "_algo_enqueue(" not in configured


def test_remaining_replacement_producers_are_not_claimed_complete():
    source = (ROOT / "shared/position_manager.py").read_text()
    for marker in ("def _update_stop_loss(", "def _place_trail_sl("):
        body = source.split(marker, 1)[1].split("\n\ndef ", 1)[0]
        assert "_algo_enqueue_native_open(" not in body
    model = MODEL.read_text()
    assert "break-even, trailing" in model
    assert "remain legacy/unwired" in model
    assert "close finalization is not wired" in model


def test_docs_mark_only_r4a_closed():
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    assert "**P10 R4A PASS.**" in model
    assert "R4A Native Active Open | **IMPLEMENTED / CLOSED**" in backlog
    assert "R4 Active Producer Wiring | IN_PROGRESS" in backlog
