"""R4B-STATE-ACK guards for strict typed snapshot CAS."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODEL = ROOT / "docs/v2/P10_R4B_TYPED_SNAPSHOT_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def test_strict_snapshot_is_single_key_raw_token_cas():
    source = (ROOT / "position_state/strict_snapshot.py").read_text()
    assert 'PM_POSITIONS_KEY = "pm:positions"' in source
    assert "STRICT_SNAPSHOT_CAS_LUA,\n                1," in source
    assert "if current == proposed then" in source
    assert "elseif current ~= expected then" in source
    assert "redis.call('SET', KEYS[1], proposed)" in source


def test_read_and_write_outcomes_are_explicit_and_distinct():
    source = (ROOT / "position_state/strict_snapshot.py").read_text()
    for code in ("FOUND", "NOT_FOUND", "MALFORMED", "UNAVAILABLE"):
        assert f'{code} = "{code}"' in source
    for code in (
        "APPLIED",
        "ALREADY_APPLIED",
        "STALE",
        "UNKNOWN",
        "INVALID",
    ):
        assert f'{code} = "{code}"' in source
    assert "ACK can be ambiguous" in source


def test_strict_snapshot_has_no_file_fallback_or_legacy_helper_import():
    source = (ROOT / "position_state/strict_snapshot.py").read_text()
    for forbidden in (
        "pathlib",
        "open(",
        "write_text",
        "shared.redis_store",
        "RedisPositionStateAdapter",
    ):
        assert forbidden not in source


def test_legacy_position_state_contract_is_unchanged():
    port = (ROOT / "execution/ports/position_state.py").read_text()
    adapter = (ROOT / "execution/adapters/position_state.py").read_text()
    service = (ROOT / "position_state/service.py").read_text()
    assert "def save_positions(self, positions: dict) -> None:" in port
    assert "def save_positions(self, positions: dict) -> None:" in adapter
    assert "def save(self, positions: dict) -> None:" in service
    assert "StrictRedisPositionSnapshotAdapter" not in port + adapter + service


def test_typed_adapter_remains_dormant_and_docs_preserve_gate():
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_state"):
            continue
        if "StrictRedisPositionSnapshotAdapter" in path.read_text():
            callers.append(str(relative))
    assert callers == []
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    assert "No runtime caller uses this strict adapter yet" in model
    assert "active R4B migration" in model
    assert "R4B-STATE-ACK Typed Snapshot CAS | **IMPLEMENTED / CLOSED (DORMANT)**" in backlog
    assert "R4 Active Producer Wiring | IN_PROGRESS" in backlog
