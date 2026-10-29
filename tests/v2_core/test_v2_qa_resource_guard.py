"""Exercise the QA shell lifecycle with fake services, never a real database."""

import fcntl
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def runner(tmp_path):
    if os.getuid() == 0:
        pytest.skip("data QA deliberately refuses root")
    script = tmp_path / "scripts" / "qa_v2_data_core.sh"
    script.parent.mkdir()
    shutil.copyfile(
        Path(__file__).resolve().parents[2] / "scripts" / script.name, script
    )
    programs = tmp_path / "bin"
    programs.mkdir()
    bodies = {
        "initdb": 'mkdir "$2"; touch "$2/PG_VERSION"',
        "pg_ctl": """case " $* " in
        *" start "*) touch "$2/postmaster.pid";;
        *" stop "*) if [ "${QA_FAKE_STOP_ERROR:-0}" = 1 ]; then exit 1; fi; rm "$2/postmaster.pid";;
        esac""",
        "df": 'echo "Filesystem Size Used Avail Use% Mounted"; echo "fake 0 0 ${QA_FAKE_FREE:-21474836480} 0% /tmp"',
        "qa-python": 'echo "FAKE PYTEST (no database started)"; exit "${QA_FAKE_EXIT:-0}"',
    }
    for name in ("pg_dump", "pg_restore", "redis-server", "redis-cli", "clickhouse"):
        bodies[name] = "exit 0"
    for name, body in bodies.items():
        program = programs / name
        program.write_text("#!/usr/bin/env bash\nset -eu\n" + body + "\n")
        program.chmod(0o700)
    artifacts = []

    def run(**changes):
        env = {
            **os.environ,
            "PATH": str(programs) + ":" + os.environ["PATH"],
            "V2_QA_POSTGRES_BIN": str(programs),
            "V2_QA_PYTHON": "qa-python",
            "V2_QA_KEEP_DATABASE": "0",
            "V2_QA_HOST_LOCK": str(tmp_path / "host.lock"),
            **changes,
        }
        result = subprocess.run(
            ["bash", str(script)],
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        match = re.search(
            r"diagnostics retained at (/tmp/v2-data-qa\.[A-Za-z0-9]{6});", result.stdout
        )
        root = Path(match[1]) if match else None
        if root:
            artifacts.append(root)
        return result, root

    yield run, tmp_path
    for root in artifacts:
        # Only the fake, per-test mktemp directories discovered above.
        shutil.rmtree(root)


@pytest.mark.parametrize(
    "exit_code,keep,retained", [(0, "0", False), (7, "0", False), (7, "1", True)]
)
def test_qa_reclaims_own_database_but_keeps_log_and_exit_status(
    runner, exit_code, keep, retained
):
    run, _ = runner
    result, root = run(QA_FAKE_EXIT=str(exit_code), V2_QA_KEEP_DATABASE=keep)
    assert result.returncode == exit_code, result.stderr
    assert (root / "pg").exists() is retained
    assert (root / "KEEP_DATABASE").exists() is (keep == "1")
    assert "FAKE PYTEST" in (root / "pytest.log").read_text()


def test_failed_shutdown_retains_database_for_diagnosis(runner):
    result, root = runner[0](QA_FAKE_STOP_ERROR="1")
    assert result.returncode == 2
    assert (root / "pg" / "postmaster.pid").exists()


def test_low_disk_refuses_before_starting_any_service(runner):
    result, root = runner[0](QA_FAKE_FREE="8589934592")
    assert result.returncode == 2 and root is None
    assert "at least 10 GiB" in result.stderr


def test_concurrent_qa_is_refused(runner):
    run, repo = runner
    cache = repo / ".pytest_cache"
    cache.mkdir()
    with (cache / "v2-data-qa.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result, root = run()
    assert result.returncode == 2 and root is None
    assert "already running" in result.stderr


def test_host_lock_also_blocks_other_checkouts_and_cleanup(runner):
    run, repo = runner
    with (repo / "host.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result, root = run()
    assert result.returncode == 2 and root is None
    assert "host QA or cleanup" in result.stderr
