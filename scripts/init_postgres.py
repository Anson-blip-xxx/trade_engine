#!/usr/bin/env python3
"""Initialize the PostgreSQL trading ledger from db/postgres_schema.sql."""
from pathlib import Path
import os


def main():
    import psycopg

    dsn = os.environ.get('POSTGRES_DSN', '').strip()
    if not dsn:
        raise SystemExit('POSTGRES_DSN is required')
    schema = (Path(__file__).resolve().parent.parent / 'db/postgres_schema.sql').read_text()
    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(schema)
    print('PostgreSQL trading ledger initialized')


if __name__ == '__main__':
    main()
