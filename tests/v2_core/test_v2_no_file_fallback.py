import pytest

from shared import redis_store


def test_failed_redis_never_reads_or_writes_files(monkeypatch):
    monkeypatch.setattr(redis_store, "_check_redis", lambda: False)
    for action in (
        lambda: redis_store.get("pm:positions"),
        lambda: redis_store.set("pm:positions", {}),
        lambda: redis_store.delete("pm:positions"),
        lambda: redis_store.exists("pm:positions"),
    ):
        with pytest.raises(ConnectionError):
            action()
    with pytest.raises(RuntimeError, match="disabled"):
        redis_store.migrate_all()


def test_cache_write_ack_and_missing_read_have_no_file_fallback(monkeypatch):
    class Cache:
        def get(self, key):
            return None

        def set(self, key, payload):
            return True

    monkeypatch.setattr(redis_store, "_check_redis", lambda: True)
    monkeypatch.setattr(redis_store, "_conn", Cache)
    assert redis_store.get("state:s6") == {}
    assert redis_store.set("state:s6", {}, double_write=True) is True


def test_retired_s0_file_writer_fails_before_creating_a_file(tmp_path):
    from s0.adapters import S0PublisherAdapter

    target = tmp_path / "state.json"
    with pytest.raises(RuntimeError, match="disabled"):
        S0PublisherAdapter.atomic_file_write(state_file_path=str(target), state={})
    assert not target.exists()
