import os
import time

import pytest

from scripts import v2_legacy_log_gc as logs
from scripts import v2_qa_gc as qa


@pytest.fixture
def diagnostic(tmp_path, monkeypatch):
    root = tmp_path / "v2-data-qa.Aa1234"
    root.mkdir()
    path = root / "pytest.log"
    path.write_text("synthetic test diagnostics")
    os.utime(path, (time.time() - 45 * 86400,) * 2)
    monkeypatch.setattr(qa, "active_commands", list)
    monkeypatch.setattr(qa, "mounted_paths", list)
    return path


@pytest.mark.parametrize(
    "defect,expected",
    [
        ("keep", "EXPLICIT_KEEP"),
        ("dangling_keep", "EXPLICIT_KEEP"),
        ("pg", "DATABASE_PRESENT"),
        ("socket", "PID_OR_SOCKET_PRESENT"),
        ("recent", "TOO_RECENT"),
        ("process", "PROCESS_REFERENCE"),
        ("mount", "MOUNT_PRESENT"),
        ("hardlink", "NOT_PRIVATE_REGULAR_FILE"),
        ("symlink", "SYMLINK"),
        ("owner", "FOREIGN_OWNER"),
    ],
)
def test_diagnostics_fail_closed(diagnostic, defect, expected, monkeypatch, tmp_path):
    path = diagnostic
    root = path.parent
    commands = []
    mounts = []
    if defect == "keep":
        (root / ".keep").touch()
    elif defect == "dangling_keep":
        (root / ".keep").symlink_to(root / "missing")
    elif defect == "pg":
        (root / "pg").mkdir()
    elif defect == "socket":
        (root / "redis.sock").touch()
    elif defect == "recent":
        os.utime(path, None)
    elif defect == "process":
        commands = [str(path).encode()]
    elif defect == "mount":
        mounts = [root]
    elif defect == "hardlink":
        os.link(path, root / "another.log")
    elif defect == "symlink":
        path.unlink()
        path.symlink_to(tmp_path / "other")
    else:
        uid = os.geteuid()
        monkeypatch.setattr(qa.os, "geteuid", lambda: uid + 1)
    assert (
        qa.assess_diagnostic(
            path,
            parent=tmp_path,
            now=time.time(),
            age_seconds=30 * 86400,
            commands=commands,
            mounts=mounts,
        )
        == expected
    )


def test_only_old_allowlisted_diagnostic_files_are_removed(diagnostic, tmp_path):
    root = diagnostic.parent
    backup = root / "pg-offline.tar.gz"
    backup.write_text("do not delete backups")
    assert qa.collect_diagnostics(parent=tmp_path)[0]["result"] == "ELIGIBLE_DIAGNOSTIC"
    assert diagnostic.exists()
    records = qa.collect_diagnostics(parent=tmp_path, apply=True)
    assert records[0]["result"] == "REMOVED_DIAGNOSTIC_ONLY"
    assert not diagnostic.exists() and backup.exists() and root.exists()
    assert qa.collect_diagnostics(parent=tmp_path, apply=True) == []


def test_new_process_at_recheck_prevents_diagnostic_removal(
    diagnostic, tmp_path, monkeypatch
):
    reads = iter([[], [str(diagnostic).encode()]])
    monkeypatch.setattr(qa, "active_commands", lambda: next(reads))
    assert (
        qa.collect_diagnostics(parent=tmp_path, apply=True)[0]["result"]
        == "PROCESS_REFERENCE"
    )
    assert diagnostic.exists()


@pytest.fixture
def rotated(tmp_path):
    path = tmp_path / "clickhouse-server.log.0.gz"
    path.write_bytes(b"synthetic compressed-log fixture")
    os.utime(path, (time.time() - 45 * 86400,) * 2)
    return path


@pytest.mark.parametrize(
    "defect,expected",
    [
        ("active_name", "OUTSIDE_ROTATED_LOG_NAMESPACE"),
        ("recent", "TOO_RECENT"),
        ("open", "OPEN_FILE"),
        ("keep", "EXPLICIT_KEEP"),
        ("symlink", "SYMLINK"),
        ("hardlink", "FILE_IDENTITY_REJECTED"),
        ("owner", "FILE_IDENTITY_REJECTED"),
    ],
)
def test_legacy_log_namespace_and_safety(rotated, defect, expected, tmp_path):
    path = rotated
    owner = os.geteuid()
    opened = set()
    if defect == "active_name":
        path = path.rename(tmp_path / "clickhouse-server.log")
    elif defect == "recent":
        os.utime(path, None)
    elif defect == "open":
        opened = {str(path)}
    elif defect == "keep":
        (tmp_path / "KEEP_LOGS").symlink_to(tmp_path / "absent")
    elif defect == "symlink":
        path.unlink()
        path.symlink_to(tmp_path / "absent")
    elif defect == "hardlink":
        os.link(path, tmp_path / "other.gz")
    else:
        owner += 1
    assert (
        logs.assess(
            path,
            root=tmp_path,
            owner=owner,
            cutoff=time.time() - 30 * 86400,
            opened=opened,
        )
        == expected
    )


def test_legacy_log_gc_only_deletes_allowlisted_closed_old_archives(rotated, tmp_path):
    active = tmp_path / "clickhouse-server.log"
    active.write_text("active")
    business = tmp_path / "database-backup.gz"
    business.write_text("keep")
    args = {"root": tmp_path, "owner": os.geteuid(), "scan": set}
    assert logs.collect(**args)[0]["result"] == "ELIGIBLE"
    assert rotated.exists()
    removed = logs.collect(**args, apply=True)
    assert sum(r["result"] == "REMOVED_LOG_ONLY" for r in removed) == 1
    assert not rotated.exists() and active.exists() and business.exists()


def test_incomplete_process_inspection_and_new_reader_never_delete(rotated, tmp_path):
    def denied():
        raise PermissionError("test")

    with pytest.raises(PermissionError):
        logs.collect(root=tmp_path, owner=os.geteuid(), scan=denied, apply=True)
    reads = iter([set(), {str(rotated)}])
    assert (
        logs.collect(
            root=tmp_path, owner=os.geteuid(), scan=lambda: next(reads), apply=True
        )[0]["result"]
        == "OPEN_FILE"
    )
    assert rotated.exists()


@pytest.mark.parametrize("days", [0, 29, 3651, True])
def test_legacy_log_retention_is_bounded(days, tmp_path):
    with pytest.raises(ValueError):
        logs.collect(root=tmp_path, days=days, scan=set)


def test_diagnostic_and_archive_batch_limits(diagnostic, rotated, tmp_path):
    extra = diagnostic.parent / "postgres.log"
    extra.write_text("old")
    os.utime(extra, (time.time() - 45 * 86400,) * 2)
    assert (
        sum(
            r["result"] == "REMOVED_DIAGNOSTIC_ONLY"
            for r in qa.collect_diagnostics(parent=tmp_path, apply=True, limit=1)
        )
        == 1
    )
    second = tmp_path / "clickhouse-server.err.log.1.gz"
    second.write_text("old")
    os.utime(second, (time.time() - 45 * 86400,) * 2)
    assert (
        sum(
            r["result"] == "REMOVED_LOG_ONLY"
            for r in logs.collect(
                root=tmp_path, owner=os.geteuid(), scan=set, apply=True, limit=1
            )
        )
        == 1
    )
