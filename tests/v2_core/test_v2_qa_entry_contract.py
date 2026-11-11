"""Exercise the data-QA shell entry without starting a database or using TCP."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "qa_v2_data_core.sh"


@pytest.fixture
def qa_harness(tmp_path):
    repo = tmp_path / "checkout with spaces"
    scripts = repo / "scripts"
    scope = repo / "tests" / "v2_core"
    scripts.mkdir(parents=True)
    scope.mkdir(parents=True)
    entry = scripts / SCRIPT.name
    entry.write_text(SCRIPT.read_text())
    (scope / "test_sample.py").write_text("def test_sample():\n    assert True\n")
    (repo / "tests" / "test_legacy.py").write_text(
        "raise RuntimeError('legacy test loaded')\n"
    )
    (repo / "tests" / "conftest.py").write_text(
        "raise RuntimeError('legacy conftest loaded')\n"
    )
    (scope / "conftest.py").write_text("# Dedicated V2 fixture boundary.\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    driver = fake_bin / "driver"
    driver.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "name = pathlib.Path(sys.argv[0]).name\n"
        "root = pathlib.Path(os.environ['HARNESS_ROOT'])\n"
        "with (root / 'events.jsonl').open('a') as log:\n"
        "    log.write(json.dumps({'program': name, 'args': sys.argv[1:]}) + '\\n')\n"
        "if name == 'id':\n"
        "    print(os.environ.get('HARNESS_UID', '1000'))\n"
        "elif name == 'df':\n"
        "    print('Filesystem 1-blocks Used Available Capacity Mounted on')\n"
        "    print('fake 20000000000 0 ' + os.environ.get('HARNESS_FREE_BYTES', '20000000000') + ' 0% /tmp')\n"
        "elif name == 'flock':\n"
        "    sys.exit(int(os.environ.get('HARNESS_LOCK_EXIT', '0')))\n"
        "elif name == 'mktemp':\n"
        "    path = root / 'qa'\n"
        "    path.mkdir()\n"
        "    print(path)\n"
        "elif name == 'initdb':\n"
        "    pathlib.Path(sys.argv[sys.argv.index('-D') + 1]).mkdir()\n"
        "elif name == 'pg_ctl':\n"
        "    pid = pathlib.Path(sys.argv[sys.argv.index('-D') + 1]) / 'postmaster.pid'\n"
        "    if sys.argv[-1] == 'start':\n"
        "        pid.write_text('fake QA process marker')\n"
        "    elif sys.argv[-1] == 'stop':\n"
        "        pid.unlink()\n"
        "elif name == 'pytest-python':\n"
        "    keys = ['V2_CORE_TEST_DSN', 'V2_REDIS_TEST_SOCKET', 'V2_CORE_TEST_ISOLATED',\n"
        "            'V2_QA_CLUSTER_RESTART', 'PM_NO_WS', 'PYTEST_ADDOPTS', 'PYTEST_PLUGINS',\n"
        "            'PGHOSTADDR', 'PGSERVICE', 'PGSERVICEFILE']\n"
        "    (root / 'pytest.json').write_text(json.dumps({'cwd': os.getcwd(), 'args': sys.argv[1:],\n"
        "        'environment': {key: os.environ.get(key) for key in keys}}))\n"
        "    if os.environ.get('HARNESS_REAL_PYTEST') == '1':\n"
        "        os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n"
        "    print('fake pytest success')\n"
        "    sys.exit(int(os.environ.get('HARNESS_PYTEST_EXIT', '0')))\n"
    )
    driver.chmod(0o755)
    for program in (
        "id",
        "df",
        "flock",
        "mktemp",
        "initdb",
        "pg_ctl",
        "pg_dump",
        "pg_restore",
        "redis-server",
        "redis-cli",
        "clickhouse",
        "pytest-python",
    ):
        (fake_bin / program).symlink_to(driver)

    def run(args=(), *, extra_env=None):
        env = dict(os.environ)
        env.update(
            PATH=f"{fake_bin}:{os.environ['PATH']}",
            HARNESS_ROOT=str(tmp_path),
            V2_QA_POSTGRES_BIN=str(fake_bin),
            V2_QA_PYTHON=str(fake_bin / "pytest-python"),
            V2_QA_HOST_LOCK=str(tmp_path / "host.lock"),
            V2_QA_KEEP_DATABASE="0",
            PYTEST_ADDOPTS="--noconftest tests/test_legacy.py",
            PYTEST_PLUGINS="nonexistent_legacy_plugin",
            V2_CORE_TEST_DSN="must-not-use-business-dsn",
            PGHOSTADDR="192.0.2.1",
            PGSERVICE="must-not-use-business-service",
            PGSERVICEFILE="/not/a/qa/service/file",
        )
        env.update(extra_env or {})
        result = subprocess.run(
            ["bash", str(entry), *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        record = tmp_path / "pytest.json"
        events = tmp_path / "events.jsonl"
        return (
            result,
            json.loads(record.read_text()) if record.exists() else None,
            [json.loads(line) for line in events.read_text().splitlines()]
            if events.exists()
            else [],
        )

    return repo, tmp_path, run


@pytest.mark.parametrize("args", [(), ("-q",), ("--collect-only", "-q")])
def test_default_and_options_only_stay_in_v2_from_foreign_cwd(qa_harness, args):
    repo, root, run = qa_harness
    result, record, events = run(args)
    assert result.returncode == 0, result.stderr
    scope = str(repo / "tests" / "v2_core")
    assert record["cwd"] == str(repo)
    assert record["args"][:5] == [
        "-m",
        "pytest",
        f"--confcutdir={scope}",
        "-o",
        "addopts=",
    ]
    assert record["args"][-1] == scope
    environment = record["environment"]
    assert environment["PYTEST_ADDOPTS"] == environment["PYTEST_PLUGINS"] == ""
    assert (
        environment["V2_CORE_TEST_DSN"]
        == f"dbname=postgres host={root}/qa/socket port=55443"
    )
    assert environment["V2_REDIS_TEST_SOCKET"] == f"{root}/qa/redis.sock"
    assert environment["V2_CORE_TEST_ISOLATED"] == "YES"
    assert environment["V2_QA_CLUSTER_RESTART"] == "YES"
    assert all(
        environment[key] is None for key in ("PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE")
    )
    assert [event["args"] for event in events if event["program"] == "flock"] == [
        ["-n", "8"],
        ["-n", "9"],
    ]
    pg_start = next(event for event in events if event["program"] == "pg_ctl")
    assert "listen_addresses=''" in pg_start["args"][pg_start["args"].index("-o") + 1]
    redis_start = next(event for event in events if event["program"] == "redis-server")
    assert redis_start["args"][:2] == ["--port", "0"]


@pytest.mark.parametrize("cut_style", ["relative", "absolute", "split", "equivalent"])
def test_matching_explicit_confcutdir_remains_supported(qa_harness, cut_style):
    repo, _, run = qa_harness
    args = {
        "relative": ["--confcutdir=tests/v2_core"],
        "absolute": [f"--confcutdir={repo}/tests/v2_core"],
        "split": ["--confcutdir", "tests/v2_core"],
        "equivalent": ["--confcutdir=./tests/v2_core/../v2_core"],
    }[cut_style]
    result, record, _ = run([*args, "-q", "tests/v2_core/test_sample.py::test_sample"])
    assert result.returncode == 0, result.stderr
    assert sum(arg.startswith("--confcutdir") for arg in record["args"]) == 1
    assert record["args"][-1] == "tests/v2_core/test_sample.py::test_sample"


@pytest.mark.parametrize(
    "args",
    [
        ["--confcutdir=tests"],
        ["--confcutdir", "."],
        ["--confcutdir="],
        ["--confcutdir"],
        ["--noconftest"],
        ["--noconftest=true"],
        ["-o", "addopts=--noconftest"],
        ["--override-ini=testpaths=tests"],
        ["-otestpaths=tests"],
        ["-p", "legacy_plugin"],
        ["-c", "legacy.ini"],
        ["--rootdir=/tmp"],
        ["-rootdir=/tmp"],
        ["--report=A"],
        ["--report-chars=unsafe"],
        ["--report-chars", "--noconftest"],
        ["-r"],
        ["-rZ"],
        ["--pyargs", "tests"],
        ["tests/test_legacy.py"],
        ["tests"],
        ["."],
        ["tests/v2_core/../test_legacy.py"],
        ["tests/v2_core/missing.py"],
        ["-k"],
    ],
)
def test_unsafe_or_ambiguous_collection_rejected_before_provisioning(qa_harness, args):
    _, _, run = qa_harness
    result, record, events = run(args)
    assert result.returncode == 2
    assert result.stderr
    assert record is None
    assert events == []


def test_symlink_to_legacy_test_is_not_a_v2_selection(qa_harness):
    repo, _, run = qa_harness
    (repo / "tests/v2_core/linked_test.py").symlink_to(repo / "tests/test_legacy.py")
    result, record, events = run(["tests/v2_core/linked_test.py"])
    assert result.returncode == 2
    assert record is None
    assert events == []


def test_common_filters_and_explicit_absolute_selection_are_preserved(qa_harness):
    repo, _, run = qa_harness
    args = [
        "-vv",
        "-x",
        "-k",
        "sample and not slow",
        "-m",
        "not integration",
        "--maxfail",
        "1",
        "--tb=short",
        "--durations=10",
        "--collect-only",
        str(repo / "tests/v2_core/test_sample.py"),
    ]
    result, record, _ = run(args)
    assert result.returncode == 0, result.stderr
    assert record["args"][5:] == args


@pytest.mark.parametrize(
    "args, expected",
    [
        (["-rA"], "A"),
        (["-r", "fEsxXpPaAwN"], "fEsxXpPaAwN"),
        (["--report-chars=fEs"], "fEs"),
        (["--report-chars", "N"], "N"),
        (["-r", ""], ""),
    ],
)
def test_summary_flags_accept_only_known_pytest_characters(qa_harness, args, expected):
    repo, _, run = qa_harness
    result, record, _ = run(args)
    assert result.returncode == 0, result.stderr
    assert record["args"][5:] == [
        f"--report-chars={expected}",
        str(repo / "tests/v2_core"),
    ]


@pytest.mark.parametrize(
    "env, explanation",
    [
        ({"HARNESS_UID": "0"}, "unprivileged"),
        ({"HARNESS_FREE_BYTES": "10737418239"}, "10 GiB"),
        ({"HARNESS_LOCK_EXIT": "1"}, "already running"),
        ({"V2_QA_KEEP_DATABASE": "invalid"}, "must be 0 or 1"),
    ],
)
def test_existing_resource_guards_still_precede_database_start(
    qa_harness, env, explanation
):
    _, _, run = qa_harness
    result, record, events = run(extra_env=env)
    assert result.returncode == 2
    assert explanation in result.stderr
    assert record is None
    assert not {"initdb", "pg_ctl", "redis-server"}.intersection(
        event["program"] for event in events
    )


def test_pytest_failure_status_is_not_hidden_by_tee_or_cleanup(qa_harness):
    _, root, run = qa_harness
    result, record, events = run(extra_env={"HARNESS_PYTEST_EXIT": "5"})
    assert record is not None
    assert result.returncode == 5
    assert "QA exit=5" in result.stdout
    assert any(
        event["program"] == "pg_ctl" and event["args"][-1] == "stop" for event in events
    )
    assert not (root / "qa/pg/postmaster.pid").exists()


def test_real_pytest_collect_only_does_not_load_parent_legacy_conftest(qa_harness):
    _, _, run = qa_harness
    result, record, _ = run(
        ["--collect-only", "-q"], extra_env={"HARNESS_REAL_PYTEST": "1"}
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert record is not None
    assert "test_sample.py::test_sample" in result.stdout
    assert "legacy conftest loaded" not in result.stdout + result.stderr


def test_dedicated_conftest_preserves_network_audit_without_actual_connect():
    code = """
import runpy
import socket
import sys
runpy.run_path(sys.argv[1])
for family in (socket.AF_INET, socket.AF_INET6):
    with socket.socket(family) as sock:
        for event in ('socket.connect', 'socket.sendto'):
            try:
                sys.audit(event, sock, ('127.0.0.1', 1))
            except RuntimeError as error:
                assert 'external network disabled during V2 tests' in str(error)
            else:
                raise AssertionError('V2 TCP/UDP audit boundary is missing')
with socket.socket(socket.AF_UNIX) as sock:
    sys.audit('socket.connect', sock, '/not/a/real/connection')
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(REPO / "tests/v2_core/conftest.py")],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
