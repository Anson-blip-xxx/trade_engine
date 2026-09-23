"""Explicit PostgreSQL transaction factory for the independent V2 schema."""

import re
from contextlib import contextmanager


def connection_factory(dsn, *, schema, read_only=False):
    if not isinstance(dsn, str) or not dsn.strip():
        raise ValueError("explicit V2 DSN required")
    if (
        not isinstance(schema, str)
        or not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema)
        or schema in {"public", "pg_catalog", "information_schema"}
        or schema.startswith("pg_")
    ):
        raise ValueError("dedicated V2 schema required")
    if type(read_only) is not bool:
        raise ValueError("explicit read-only flag required")

    @contextmanager
    def connect():
        import psycopg
        from psycopg import sql

        with psycopg.connect(
            dsn, connect_timeout=5, application_name="trade_engine_v2"
        ) as conn:
            conn.execute(
                "SET TRANSACTION READ ONLY"
                if read_only
                else "SET TRANSACTION READ WRITE"
            )
            conn.execute(
                sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                    sql.Identifier(schema)
                )
            )
            conn.execute("SET LOCAL synchronous_commit TO on")
            # TIMESTAMPTZ remains an absolute instant; this only fixes every
            # human-readable PostgreSQL rendering to the operator timezone.
            conn.execute("SET LOCAL TIME ZONE 'Asia/Shanghai'")
            conn.execute("SET LOCAL lock_timeout TO '5s'")
            conn.execute("SET LOCAL statement_timeout TO '30s'")
            yield conn

    return connect
