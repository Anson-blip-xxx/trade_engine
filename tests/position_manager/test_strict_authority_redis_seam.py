"""Dedicated authority Redis seams must never use JSON fallback."""
import pytest

from shared import redis_store


class _Conn:
    def __init__(self):
        self.calls = []

    def get(self, key):
        self.calls.append(('get', key))
        return 'raw-authority'

    def eval(self, script, numkeys, *args):
        self.calls.append(('eval', script, numkeys, args))
        return ['OK', '']


def test_strict_authority_seams_use_raw_redis(monkeypatch):
    conn = _Conn()
    monkeypatch.setattr(redis_store, '_check_redis', lambda: True)
    monkeypatch.setattr(redis_store, '_conn', lambda: conn)

    assert redis_store.strict_get('pm:slot:v1:test') == 'raw-authority'
    assert redis_store.strict_eval('return 1', 1, 'key', 'arg') == ['OK', '']
    assert conn.calls == [
        ('get', 'pm:slot:v1:test'),
        ('eval', 'return 1', 1, ('key', 'arg')),
    ]


def test_strict_authority_seams_fail_without_file_fallback(monkeypatch):
    monkeypatch.setattr(redis_store, '_check_redis', lambda: False)
    file_reads = []
    monkeypatch.setattr(
        redis_store, '_read_file', lambda key: file_reads.append(key))

    with pytest.raises(ConnectionError, match='authority backend'):
        redis_store.strict_get('pm:slot:v1:test')
    with pytest.raises(ConnectionError, match='authority backend'):
        redis_store.strict_eval('return 1', 1, 'key')
    assert file_reads == []
