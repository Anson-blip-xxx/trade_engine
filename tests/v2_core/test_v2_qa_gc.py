import importlib.util
import os
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "qa_gc", Path(__file__).resolve().parents[2] / "scripts/v2_qa_gc.py"
)
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)


@pytest.fixture
def case(tmp_path):
    root = tmp_path / "v2-data-qa.Ab1234"
    pg = root / "pg"
    (pg / "global").mkdir(parents=True)
    (pg / "PG_VERSION").write_text("16\n")
    (pg / "global/pg_control").write_text("fake QA control")
    (pg / "postmaster.opts").write_text(
        f"postgres -D {pg} -k {root}/socket -p 55443 -c listen_addresses= -c cluster_name=v2_isolated_qa"
    )
    (root / "pytest.log").write_text("preserved diagnostics")
    old = time.time() - 172800
    for p in [*root.rglob("*"), root]:
        os.utime(p, (old, old))

    def command(*args, **kwargs):
        return SimpleNamespace(
            returncode=0, stdout="Database cluster state: shut down\n"
        )

    args = {
        "parent": tmp_path,
        "now": time.time(),
        "age_seconds": 86400,
        "commands": [],
        "mounts": [],
        "run": command,
    }
    return root, args


@pytest.mark.parametrize(
    "defect,expected",
    [
        ("pid", "PID_OR_SOCKET_PRESENT"),
        ("redis", "PID_OR_SOCKET_PRESENT"),
        ("socket", "PID_OR_SOCKET_PRESENT"),
        ("keep", "EXPLICIT_KEEP"),
        ("young", "TOO_RECENT"),
        ("process", "PROCESS_REFERENCE"),
        ("mount", "MOUNT_PRESENT"),
        ("foreign", "FOREIGN_OWNER"),
        ("production", "WRONG_CLUSTER_IDENTITY"),
        ("running", "NOT_CLEANLY_STOPPED"),
        ("symlink", "SYMLINK"),
        ("badname", "OUTSIDE_QA_NAMESPACE"),
    ],
)
def test_rejects_every_unsafe_cleanup_boundary(case, monkeypatch, defect, expected):
    root, args = case
    if defect in {"pid", "redis", "socket", "keep"}:
        path = {
            "pid": "pg/postmaster.pid",
            "redis": "redis.sock",
            "socket": "socket/.s.PGSQL.55443",
            "keep": "KEEP_DATABASE",
        }[defect]
        p = root / path
        p.parent.mkdir(exist_ok=True)
        p.touch()
        for parent in (root, root / "pg"):
            os.utime(parent, (args["now"] - 172800,) * 2)
    elif defect == "young":
        os.utime(root / "pg/global/pg_control", None)
    elif defect == "process":
        args["commands"] = [str(root / "pg").encode()]
    elif defect == "mount":
        args["mounts"] = [root / "pg/base"]
    elif defect == "foreign":
        owner = os.geteuid()
        monkeypatch.setattr(gc.os, "geteuid", lambda: owner + 1)
    elif defect == "production":
        p = root / "pg/postmaster.opts"
        p.write_text(p.read_text().replace("v2_isolated_qa", "production"))
        os.utime(p, (args["now"] - 172800,) * 2)
    elif defect == "running":
        args["run"] = lambda *a, **k: SimpleNamespace(
            returncode=0, stdout="Database cluster state: in production\n"
        )
    elif defect == "symlink":
        (root / "pg/wal-link").symlink_to("/var/lib")
        os.utime(root / "pg", (args["now"] - 172800,) * 2)
    else:
        root = root.rename(root.parent / "production")
    assert gc.assess(root, **args) == expected
    assert (root / "pg").exists()


def test_only_database_removed_and_logs_kept_and_repeated_run_safe(case, monkeypatch):
    root, args = case
    monkeypatch.setattr(gc, "active_commands", list)
    monkeypatch.setattr(gc, "mounted_paths", list)
    assert gc.assess(root, **args) == "ELIGIBLE"
    dry = gc.collect(parent=root.parent, run=args["run"])
    assert dry[0]["result"] == "ELIGIBLE" and (root / "pg").exists()
    result = gc.collect(parent=root.parent, apply=True, run=args["run"])
    assert result[0]["result"] == "REMOVED_DATABASE_ONLY"
    assert not (root / "pg").exists()
    assert (root / "pytest.log").read_text() == "preserved diagnostics"
    assert (
        gc.collect(parent=root.parent, apply=True, run=args["run"])[0]["result"]
        == "NO_DATABASE"
    )


def test_live_recheck_prevents_removal_after_initial_validation(case, monkeypatch):
    root, args = case
    reads = iter([[], [str(root).encode()]])
    monkeypatch.setattr(gc, "active_commands", lambda: next(reads))
    monkeypatch.setattr(gc, "mounted_paths", list)
    result = gc.collect(parent=root.parent, apply=True, run=args["run"])
    assert result[0]["result"] == "PROCESS_REFERENCE"
    assert (root / "pg").exists()
