"""Reclaim stopped disposable QA PostgreSQL clusters, never trading data.

CLI is dry-run unless --apply is explicit. Diagnostic logs need their own age
option; snapshots and keep markers are retained. The fixed /tmp prefix, ownership, age, process, socket, PG identity,
clean shutdown and mount/symlink checks all precede each deletion.
"""

import argparse
import fcntl
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import time
from collections import Counter
from pathlib import Path

PG_CONTROL = "/usr/lib/postgresql/16/bin/pg_controldata"


def active_commands():
    commands = []
    for item in Path("/proc").iterdir():
        if item.name.isdigit():
            try:
                commands.append((item / "cmdline").read_bytes())
            except (FileNotFoundError, PermissionError, ProcessLookupError):
                pass
    return commands


def mounted_paths():
    def decode(value):
        return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), value)

    return [
        Path(decode(line.split()[4]))
        for line in Path("/proc/self/mountinfo").read_text().splitlines()
    ]


def assess(root, *, parent, now, age_seconds, commands, mounts, run=subprocess.run):
    pg = root / "pg"
    if root.parent != parent or not re.fullmatch(
        r"v2-data-qa\.[A-Za-z0-9]{6}", root.name
    ):
        return "OUTSIDE_QA_NAMESPACE"
    if root.is_symlink() or pg.is_symlink() or root.resolve() != root:
        return "SYMLINK"
    if not pg.is_dir():
        return "NO_DATABASE"
    if any(
        (root / marker).exists() or (root / marker).is_symlink()
        for marker in ("KEEP_DATABASE", ".keep")
    ):
        return "EXPLICIT_KEEP"
    paths = (
        root,
        pg,
        pg / "postmaster.opts",
        pg / "PG_VERSION",
        pg / "global" / "pg_control",
    )
    try:
        details = [p.lstat() for p in paths]
    except FileNotFoundError:
        return "INCOMPLETE_IDENTITY"
    if any(s.st_uid != os.geteuid() for s in details):
        return "FOREIGN_OWNER"
    if any(stat.S_ISLNK(s.st_mode) for s in details):
        return "SYMLINK"
    if now - max(s.st_mtime for s in details) < age_seconds:
        return "TOO_RECENT"
    if any(
        (root / p).exists() or (root / p).is_symlink()
        for p in (
            "pg/postmaster.pid",
            "redis.pid",
            "redis.sock",
            "socket/.s.PGSQL.55443",
            "socket/.s.PGSQL.55443.lock",
        )
    ):
        return "PID_OR_SOCKET_PRESENT"
    if any(str(root).encode() in command for command in commands):
        return "PROCESS_REFERENCE"
    if any(m == root or root in m.parents for m in mounts):
        return "MOUNT_PRESENT"
    try:
        args = shlex.split((pg / "postmaster.opts").read_text())
        expected = {"-D": str(pg), "-k": str(root / "socket"), "-p": "55443"}
        if any(
            args.count(k) != 1 or args[args.index(k) + 1] != v
            for k, v in expected.items()
        ):
            return "WRONG_CLUSTER_IDENTITY"
        if (
            "cluster_name=v2_isolated_qa" not in args
            or "listen_addresses=" not in args
            or (pg / "PG_VERSION").read_text().strip() != "16"
        ):
            return "WRONG_CLUSTER_IDENTITY"
    except (ValueError, IndexError, OSError):
        return "WRONG_CLUSTER_IDENTITY"
    for folder, directories, files in os.walk(pg, followlinks=False):
        for name in directories + files:
            p = Path(folder) / name
            if p.is_symlink():
                return "SYMLINK"
            if p.stat().st_dev != details[1].st_dev:
                return "MOUNT_PRESENT"
    result = run(
        [PG_CONTROL, str(pg)],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode or not re.search(
        r"^Database cluster state:\s+shut down\s*$", result.stdout, re.MULTILINE
    ):
        return "NOT_CLEANLY_STOPPED"
    return "ELIGIBLE"


def collect(
    *,
    parent=Path("/tmp"),
    apply=False,
    age_seconds=86400,
    limit=256,
    run=subprocess.run,
):
    now, commands, mounts = time.time(), active_commands(), mounted_paths()
    records = []
    count = 0
    for root in sorted(parent.glob("v2-data-qa.*")):
        try:
            reason = assess(
                root,
                parent=parent,
                now=now,
                age_seconds=age_seconds,
                commands=commands,
                mounts=mounts,
                run=run,
            )
            if reason == "ELIGIBLE" and count >= limit:
                reason = "BATCH_LIMIT"
            if reason == "ELIGIBLE":
                count += 1
                if apply:
                    # Recheck immediately; new runners share the host lock, old
                    # runners are still fenced by process/PID/socket/age checks.
                    reason = assess(
                        root,
                        parent=parent,
                        now=time.time(),
                        age_seconds=age_seconds,
                        commands=active_commands(),
                        mounts=mounted_paths(),
                        run=run,
                    )
                    if reason == "ELIGIBLE":
                        if not shutil.rmtree.avoids_symlink_attacks:
                            raise RuntimeError("SAFE_RMTREE_REQUIRED")
                        shutil.rmtree(root / "pg")
                        reason = "REMOVED_DATABASE_ONLY"
            records.append({"directory": str(root), "result": reason})
        except (OSError, subprocess.SubprocessError, ValueError):
            records.append({"directory": str(root), "result": "CHECK_FAILED_RETAINED"})
    return records


DIAGNOSTIC_FILES = ("pytest.log", "initdb.log", "postgres.log", "redis.log")


def assess_diagnostic(path, *, parent, now, age_seconds, commands, mounts):
    root = path.parent
    if (
        root.parent != parent
        or not re.fullmatch(r"v2-data-qa\.[A-Za-z0-9]{6}", root.name)
        or path.name not in DIAGNOSTIC_FILES
    ):
        return "OUTSIDE_DIAGNOSTIC_NAMESPACE"
    if root.is_symlink() or root.resolve() != root or path.is_symlink():
        return "SYMLINK"
    if any(
        (root / m).exists() or (root / m).is_symlink()
        for m in ("KEEP_DATABASE", ".keep")
    ):
        return "EXPLICIT_KEEP"
    if (root / "pg").exists() or (root / "pg").is_symlink():
        return "DATABASE_PRESENT"
    if any(
        (root / p).exists() or (root / p).is_symlink()
        for p in (
            "redis.pid",
            "redis.sock",
            "socket/.s.PGSQL.55443",
            "socket/.s.PGSQL.55443.lock",
        )
    ):
        return "PID_OR_SOCKET_PRESENT"
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or details.st_nlink != 1:
        return "NOT_PRIVATE_REGULAR_FILE"
    if root.stat().st_uid != os.geteuid() or details.st_uid != os.geteuid():
        return "FOREIGN_OWNER"
    if now - details.st_mtime < age_seconds:
        return "TOO_RECENT"
    if any(str(root).encode() in command for command in commands):
        return "PROCESS_REFERENCE"
    if any(m == root or root in m.parents for m in mounts):
        return "MOUNT_PRESENT"
    return "ELIGIBLE_DIAGNOSTIC"


def collect_diagnostics(*, parent=Path("/tmp"), apply=False, days=30, limit=256):
    if (
        type(days) is not int
        or not 14 <= days <= 3650
        or type(limit) is not int
        or not 1 <= limit <= 256
    ):
        raise ValueError("DIAGNOSTIC_RETENTION_BOUNDS")
    records = []
    count = 0
    commands, mounts = active_commands(), mounted_paths()
    for root in sorted(parent.glob("v2-data-qa.*")):
        for name in DIAGNOSTIC_FILES:
            path = root / name
            if not path.exists() and not path.is_symlink():
                continue
            try:
                reason = assess_diagnostic(
                    path,
                    parent=parent,
                    now=time.time(),
                    age_seconds=days * 86400,
                    commands=commands,
                    mounts=mounts,
                )
                size = 0
                if reason == "ELIGIBLE_DIAGNOSTIC":
                    if count >= limit:
                        reason = "BATCH_LIMIT"
                    else:
                        count += 1
                        size = path.lstat().st_size
                        if apply:
                            reason = assess_diagnostic(
                                path,
                                parent=parent,
                                now=time.time(),
                                age_seconds=days * 86400,
                                commands=active_commands(),
                                mounts=mounted_paths(),
                            )
                            if reason == "ELIGIBLE_DIAGNOSTIC":
                                before = path.lstat()
                                fd = os.open(
                                    root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                )
                                try:
                                    actual = os.stat(
                                        name, dir_fd=fd, follow_symlinks=False
                                    )
                                    if (
                                        actual.st_ino,
                                        actual.st_dev,
                                        actual.st_mtime_ns,
                                    ) != (
                                        before.st_ino,
                                        before.st_dev,
                                        before.st_mtime_ns,
                                    ):
                                        reason = "FILE_CHANGED_RETAINED"
                                    else:
                                        os.unlink(name, dir_fd=fd)
                                        reason = "REMOVED_DIAGNOSTIC_ONLY"
                                finally:
                                    os.close(fd)
                records.append({"file": str(path), "result": reason, "bytes": size})
            except (OSError, ValueError):
                records.append({"file": str(path), "result": "CHECK_FAILED_RETAINED"})
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--min-age-hours", type=int, default=24)
    parser.add_argument("--limit", type=int, default=256)
    parser.add_argument("--diagnostic-days", type=int)
    args = parser.parse_args()
    if (
        os.geteuid() == 0
        or not 24 <= args.min_age_hours <= 8760
        or not 1 <= args.limit <= 256
        or (args.diagnostic_days is not None and not 14 <= args.diagnostic_days <= 3650)
    ):
        parser.error("unprivileged owner, age >=24h and batch <=256 required")
    lock_path = f"/tmp/trade-v2-qa-host-{os.geteuid()}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"status": "SKIPPED", "reason": "QA_OR_CLEANUP_RUNNING"}))
            return
        records = collect(
            apply=args.apply, age_seconds=args.min_age_hours * 3600, limit=args.limit
        )
        if args.diagnostic_days is not None:
            records += collect_diagnostics(
                apply=args.apply, days=args.diagnostic_days, limit=args.limit
            )
        for record in records:
            if record["result"].startswith(("REMOVED", "ELIGIBLE")):
                print(json.dumps(record), flush=True)
        print(
            json.dumps(
                {
                    "status": "APPLIED" if args.apply else "DRY_RUN",
                    "removed": sum(
                        r["result"] == "REMOVED_DATABASE_ONLY" for r in records
                    ),
                    "diagnostics_removed": sum(
                        r["result"] == "REMOVED_DIAGNOSTIC_ONLY" for r in records
                    ),
                    "outcomes": dict(Counter(r["result"] for r in records)),
                }
            )
        )


if __name__ == "__main__":
    main()
