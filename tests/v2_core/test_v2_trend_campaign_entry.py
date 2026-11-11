from contextlib import contextmanager

import pytest

from services.v2_trend_campaign_entry import LOCK, run


class Connection:
    def __init__(self, acquired=True):
        self.acquired, self.sql, self.commits = acquired, [], 0

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        return self

    def fetchone(self):
        return (self.acquired,)

    def commit(self):
        self.commits += 1


class Archive:
    closed = False

    def close(self):
        self.closed = True


def ports(acquired=True):
    conn, archive = Connection(acquired), Archive()

    @contextmanager
    def base():
        yield conn

    return conn, archive, base


def test_lock_committed_before_worker_and_archive_closed():
    conn, archive, base = ports()

    def worker(connect, supplied, *, now_ms):
        assert supplied is archive and conn.commits == 1 and now_ms == 123
        return {"status": "COMPLETE", "execution_authorized": False}

    result = run(
        {}, base=base, archive_factory=lambda: archive, worker=worker, now_ms=123
    )
    assert result["status"] == "COMPLETE" and archive.closed
    assert conn.sql[-1][1] == (LOCK,) and "pg_advisory_unlock" in conn.sql[-1][0]


def test_duplicate_worker_does_not_open_archive():
    conn, archive, base = ports(False)

    def forbidden():
        raise AssertionError("archive should not open")

    assert (
        run({}, base=base, archive_factory=forbidden, worker=forbidden)["status"]
        == "ALREADY_RUNNING"
    )
    assert not archive.closed and conn.commits == 1


def test_worker_failure_releases_resources():
    conn, archive, base = ports()

    def fail(*args, **kwargs):
        raise ValueError("injected")

    with pytest.raises(ValueError, match="injected"):
        run({}, base=base, archive_factory=lambda: archive, worker=fail)
    assert archive.closed and "pg_advisory_unlock" in conn.sql[-1][0]
