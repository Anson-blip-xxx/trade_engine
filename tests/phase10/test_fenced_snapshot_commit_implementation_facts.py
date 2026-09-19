"""R4B-COMPOSE guards for the dormant atomic fenced snapshot commit."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "position_state/fenced_snapshot.py"
MODEL = ROOT / "docs/v2/P10_R4B_FENCED_SNAPSHOT_COMMIT_IMPLEMENTATION.md"
BACKLOG = ROOT / "docs/v2/PHASE10_BACKLOG.md"


def test_lua_fences_all_canonical_tokens_before_snapshot_comparison():
    source = SOURCE.read_text()
    canonical_loop = source.index("for index = 2, #KEYS do")
    snapshot_read = source.index("local current_snapshot = redis.call('GET', KEYS[1])")
    assert canonical_loop < snapshot_read
    assert "local expected = ARGV[index + 2]" in source
    assert "FENCED_SNAPSHOT_COMMIT_LUA,\n                len(keys)," in source


def test_lua_only_mutates_the_legacy_snapshot_key():
    source = SOURCE.read_text()
    assert "redis.call('SET', KEYS[1], proposed_snapshot)" in source
    assert source.count("redis.call('SET'") == 1
    assert "redis.call('SET', KEYS[index]" not in source


def test_batch_coverage_and_planner_composition_are_explicit():
    source = SOURCE.read_text()
    assert "symbols = set(current_snapshot) | set(proposed_snapshot)" in source
    assert "set(slots) != symbols" in source
    assert "slots must exactly cover current/proposed symbols" in source
    assert "plan = plan_legacy_snapshot_write(" in source
    assert "encoded = StrictRedisPositionSnapshotAdapter._encode(planned)" in source


def test_adapter_remains_dormant_and_roadmap_preserves_active_gate():
    callers = []
    for path in ROOT.rglob("*.py"):
        relative = path.relative_to(ROOT)
        if relative.parts[0] in ("tests", "position_state"):
            continue
        if "RedisFencedSnapshotCommitAdapter" in path.read_text():
            callers.append(str(relative))
    assert callers == []
    model = MODEL.read_text()
    backlog = BACKLOG.read_text()
    assert "No runtime\n> caller uses this adapter" in model
    assert "Active R4B migration remains `IN_PROGRESS`" in model
    assert "R4B-COMPOSE Atomic Fenced Snapshot Commit | **IMPLEMENTED / CLOSED (DORMANT)**" in backlog
    assert "R4 Active Producer Wiring | IN_PROGRESS" in backlog
