"""The shared test builders must preserve the isolated database boundary."""

import ast
from pathlib import Path
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg import sql

from tests.support import v2_admission as support

ROOT = Path(__file__).resolve().parents[2]


def test_support_is_not_a_collected_test_module_or_legacy_fixture_import():
    tree = ast.parse(Path(support.__file__).read_text())
    assert not any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
        for node in tree.body
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert "test_" not in (node.module or "")
            assert "conftest" not in (node.module or "")
        elif isinstance(node, ast.Import):
            assert all("conftest" not in alias.name for alias in node.names)
    assert Path(support.__file__).resolve().parents[2] == ROOT
    assert (ROOT / "db/postgres_v2_core_schema.sql").is_file()


def test_v2_no_longer_imports_the_admission_test_module():
    for path in (ROOT / "tests/v2_core").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[-1] != (
                    "test_v2_intent_admission"
                ), path
            elif isinstance(node, ast.Import):
                assert all(
                    alias.name.split(".")[-1] != "test_v2_intent_admission"
                    for alias in node.names
                ), path


@pytest.mark.parametrize(
    ("dsn", "isolated"),
    [
        (None, None),
        (None, "YES"),
        ("host=/tmp/v2-data-qa.fake/socket dbname=postgres", None),
        ("host=/tmp/v2-data-qa.fake/socket dbname=postgres", "NO"),
    ],
)
def test_shared_fixture_requires_explicit_isolation(monkeypatch, dsn, isolated):
    for key, value in (("V2_CORE_TEST_DSN", dsn), ("V2_CORE_TEST_ISOLATED", isolated)):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        psycopg, "connect", lambda *_a, **_k: pytest.fail("must not connect")
    )
    with pytest.raises(pytest.skip.Exception, match="explicit isolated"):
        next(support.database.__wrapped__())


@pytest.mark.parametrize(
    "dsn",
    [
        "dbname=postgres host=127.0.0.1",
        "dbname=postgres host=localhost",
        "dbname=postgres host=/var/run/postgresql",
        "dbname=postgres",
        "dbname=postgres host=/tmp/not-a-v2-test/socket",
        "dbname=postgres host=/tmp/v2-data-qa.fake/socket,/var/run/postgresql",
        "dbname=postgres host=/tmp/v2-data-qa.fake/../../var/run/postgresql",
        "dbname=postgres host=/tmp/v2-data-qa.fake/socket hostaddr=127.0.0.1",
        "dbname=postgres host=/tmp/v2-data-qa.fake/socket service=business",
    ],
)
def test_shared_fixture_rejects_business_endpoints_before_connect(monkeypatch, dsn):
    monkeypatch.setenv("V2_CORE_TEST_DSN", dsn)
    monkeypatch.setenv("V2_CORE_TEST_ISOLATED", "YES")
    monkeypatch.setattr(
        psycopg, "connect", lambda *_a, **_k: pytest.fail("must not connect")
    )
    with pytest.raises(pytest.fail.Exception, match="dedicated temporary Unix socket"):
        next(support.database.__wrapped__())


@pytest.mark.parametrize("variable", ["PGHOSTADDR", "PGSERVICE", "PGSERVICEFILE"])
def test_shared_fixture_rejects_libpq_endpoint_overrides(monkeypatch, variable):
    monkeypatch.setenv("V2_CORE_TEST_DSN", "host=/tmp/v2-data-qa.fake/socket")
    monkeypatch.setenv("V2_CORE_TEST_ISOLATED", "YES")
    monkeypatch.setenv(variable, "must-not-be-used")
    monkeypatch.setattr(
        psycopg, "connect", lambda *_a, **_k: pytest.fail("must not connect")
    )
    with pytest.raises(pytest.fail.Exception, match="dedicated temporary Unix socket"):
        next(support.database.__wrapped__())


def test_shared_fixture_rejects_symlink_escape(monkeypatch, tmp_path):
    folder = Path("/tmp") / ("v2-data-qa." + uuid4().hex)
    folder.mkdir(mode=0o700)
    socket = folder / "socket"
    try:
        socket.symlink_to(tmp_path, target_is_directory=True)
        monkeypatch.setenv("V2_CORE_TEST_DSN", f"host={socket}")
        monkeypatch.setenv("V2_CORE_TEST_ISOLATED", "YES")
        monkeypatch.setattr(
            psycopg, "connect", lambda *_a, **_k: pytest.fail("must not connect")
        )
        with pytest.raises(
            pytest.fail.Exception, match="dedicated temporary Unix socket"
        ):
            next(support.database.__wrapped__())
    finally:
        socket.unlink(missing_ok=True)
        folder.rmdir()


def test_shared_fixture_checks_cluster_identity_before_creating_schema(monkeypatch):
    monkeypatch.setenv("V2_CORE_TEST_DSN", "host=/tmp/v2-data-qa.fake/socket")
    monkeypatch.setenv("V2_CORE_TEST_ISOLATED", "YES")
    statements = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement):
            statements.append(statement)
            return self

        def fetchone(self):
            return ("not-an-isolated-qa-cluster",)

    monkeypatch.setattr(psycopg, "connect", lambda *_a, **_k: Connection())
    with pytest.raises(pytest.fail.Exception, match="isolated QA cluster identity"):
        next(support.database.__wrapped__())
    assert statements == ["SHOW cluster_name"]


@pytest.mark.parametrize("fail_seed", [False, True])
def test_shared_fixture_cleans_only_its_schema_even_if_seeding_fails(
    monkeypatch, fail_seed
):
    dsn = "host=/tmp/v2-data-qa.fake/socket dbname=postgres"
    monkeypatch.setenv("V2_CORE_TEST_DSN", dsn)
    monkeypatch.setenv("V2_CORE_TEST_ISOLATED", "YES")
    identity = UUID(int=1)
    monkeypatch.setattr(support, "uuid4", lambda: identity)
    statements = []
    connections = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement):
            statements.append(statement)
            return self

        def fetchone(self):
            return ("v2_isolated_qa",)

    def connect(value):
        assert value == dsn
        connections.append(value)
        return Connection()

    def seed(_store, item):
        assert item == support.evidence()
        if fail_seed:
            raise RuntimeError("fake seed failed")

    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(support.EvidenceStore, "put", seed)
    fixture = support.database.__wrapped__()
    if fail_seed:
        with pytest.raises(RuntimeError, match="fake seed failed"):
            next(fixture)
    else:
        factory = next(fixture)
        with factory() as conn:
            conn.execute("SELECT 1")
        fixture.close()
    assert statements[0] == "SHOW cluster_name"
    name = sql.Identifier("v2_core_test_" + identity.hex)
    assert statements[1] == sql.SQL("CREATE SCHEMA {}").format(name)
    assert statements[2] == sql.SQL("SET search_path TO {}").format(name)
    assert statements[3] == (ROOT / "db/postgres_v2_core_schema.sql").read_text()
    assert statements[-1] == sql.SQL("DROP SCHEMA {} CASCADE").format(name)
    assert len(connections) == (2 if fail_seed else 3)
