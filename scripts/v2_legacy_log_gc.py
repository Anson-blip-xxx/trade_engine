"""Only expired rotated ClickHouse log gzip files, never database/archive payloads."""

import argparse
import json
import os
import pwd
import re
import stat
import time
from pathlib import Path

ROOT = Path("/var/log/clickhouse-server")
NAME = re.compile(r"clickhouse-server(?:\.err)?\.log\.[0-9]+\.gz")


def open_files():
    result = set()
    for process in Path("/proc").iterdir():
        if not process.name.isdigit():
            continue
        try:
            for fd in (process / "fd").iterdir():
                try:
                    result.add(os.readlink(fd))
                except FileNotFoundError:
                    pass
        except (FileNotFoundError, ProcessLookupError):
            pass
        # PermissionError is not a proof of inactivity: propagate and retain.
    return result


def assess(path, *, root, owner, cutoff, opened):
    if path.parent != root or not NAME.fullmatch(path.name):
        return "OUTSIDE_ROTATED_LOG_NAMESPACE"
    if root.is_symlink() or root.resolve() != root or path.is_symlink():
        return "SYMLINK"
    if any(
        (root / m).exists() or (root / m).is_symlink() for m in ("KEEP_LOGS", ".keep")
    ):
        return "EXPLICIT_KEEP"
    details = path.lstat()
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_nlink != 1
        or details.st_uid != owner
        or root.stat().st_uid != owner
    ):
        return "FILE_IDENTITY_REJECTED"
    if details.st_mtime >= cutoff:
        return "TOO_RECENT"
    if str(path) in opened:
        return "OPEN_FILE"
    return "ELIGIBLE"


def collect(
    *,
    apply=False,
    days=30,
    limit=20,
    root=ROOT,
    owner=None,
    scan=open_files,
    report=None,
):
    if (
        type(days) is not int
        or not 30 <= days <= 3650
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ValueError("LOG_RETENTION_BOUNDS")
    if owner is None:
        owner = pwd.getpwnam("clickhouse").pw_uid
    cutoff = time.time() - days * 86400
    opened = scan()
    records = []
    eligible = 0
    for path in sorted(root.glob("*.gz")):
        reason = assess(path, root=root, owner=owner, cutoff=cutoff, opened=opened)
        size = 0
        if reason == "ELIGIBLE":
            if eligible >= limit:
                reason = "BATCH_LIMIT"
            else:
                eligible += 1
                size = path.lstat().st_size
                if apply:
                    reason = assess(
                        path, root=root, owner=owner, cutoff=cutoff, opened=scan()
                    )
                    if reason == "ELIGIBLE":
                        before = path.lstat()
                        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                        try:
                            current = os.stat(
                                path.name, dir_fd=fd, follow_symlinks=False
                            )
                            if (
                                current.st_dev,
                                current.st_ino,
                                current.st_mtime_ns,
                            ) != (before.st_dev, before.st_ino, before.st_mtime_ns):
                                reason = "CHANGED_RETAINED"
                            else:
                                os.unlink(path.name, dir_fd=fd)
                                reason = "REMOVED_LOG_ONLY"
                        finally:
                            os.close(fd)
        records.append({"file": str(path), "result": reason, "bytes": size})
        if report is not None:
            report(records[-1])
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()
    try:
        records = collect(
            apply=args.apply,
            days=args.days,
            report=lambda record: print(json.dumps(record), flush=True),
        )
        print(
            json.dumps(
                {
                    "status": "APPLIED" if args.apply else "DRY_RUN",
                    "removed": sum(r["result"] == "REMOVED_LOG_ONLY" for r in records),
                    "removed_bytes": sum(
                        r["bytes"] for r in records if r["result"] == "REMOVED_LOG_ONLY"
                    ),
                }
            ),
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 - uncertain inspection never deletes
        print(
            json.dumps(
                {
                    "status": "FAILED_CHECK_LOG_FOR_PARTIAL_RESULTS",
                    "error_type": type(exc).__name__,
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
