import os
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from v2_core.intents import AdmissionCode as Code
from v2_core.intents import IntentStore, OpenIntent


def intent():
    return OpenIntent(
        str(uuid4()), 'BINANCE', 'test-account', 'SANDBOX', 'FUTURES',
        's6', str(uuid4()), 'BTCUSDT', 'BUY', '0.0100', 'strategy-v1',
        'a' * 64, 'evidence-1')


@pytest.mark.parametrize('quantity', [0.1, 'NaN', 'Infinity', '-1', '0',
                                     '1e20', '1e-19', 'bad'])
def test_reject_invalid_quantity(quantity):
    with pytest.raises(ValueError):
        replace(intent(), quantity=quantity)


def test_quantity_is_canonical_and_precision_preserved():
    assert intent().quantity == '0.01'
    assert replace(intent(), quantity='1e-18').quantity == '0.000000000000000001'
    assert replace(intent(), quantity='10').quantity == '10'


@pytest.fixture
def database():
    dsn = os.environ.get('V2_CORE_TEST_DSN')
    if not dsn or os.environ.get('V2_CORE_TEST_ISOLATED') != 'YES':
        pytest.skip('requires explicit isolated QA database')
    import psycopg
    from psycopg import sql

    name = 'v2_core_test_' + uuid4().hex
    ddl = (Path(__file__).resolve().parents[2] /
           'db/postgres_v2_core_schema.sql').read_text()
    with psycopg.connect(dsn) as conn:
        conn.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(name)))
        conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(name)))
        conn.execute(ddl)

    @contextmanager
    def connect():
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL('SET search_path TO {}').format(sql.Identifier(name)))
            yield conn

    try:
        yield connect
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(name)))


def counts(connect):
    with connect() as conn:
        return conn.execute('SELECT (SELECT count(*) FROM v2_trade_intents), '
                            '(SELECT count(*) FROM v2_domain_outbox)').fetchone()


def test_different_uuid_same_request_returns_original(database):
    store = IntentStore(database)
    original = intent()
    assert store.admit(original).code is Code.ACCEPTED
    retry = store.admit(replace(original, intent_id=str(uuid4()), quantity='1e-2'))
    assert retry.code is Code.ALREADY_ACCEPTED
    assert retry.intent_id == original.intent_id
    assert counts(database) == (1, 1)


def test_concurrent_request_has_single_intent_and_event(database):
    store = IntentStore(database)
    original = intent()
    requests = [replace(original, intent_id=str(uuid4())) for _ in range(8)]
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(store.admit, requests))
    assert sum(r.code is Code.ACCEPTED for r in results) == 1
    assert sum(r.code is Code.ALREADY_ACCEPTED for r in results) == 7
    assert len({r.intent_id for r in results}) == 1
    assert counts(database) == (1, 1)


def test_payload_conflict_and_account_isolation(database):
    store = IntentStore(database)
    original = intent()
    assert store.admit(original).code is Code.ACCEPTED
    conflict = replace(original, intent_id=str(uuid4()), quantity='2')
    assert store.admit(conflict).code is Code.CONFLICT
    separate = replace(original, intent_id=str(uuid4()), account_id='other')
    assert store.admit(separate).code is Code.ACCEPTED
    assert counts(database) == (2, 2)


def test_uuid_collision_cannot_create_second_request(database):
    store = IntentStore(database)
    original = intent()
    assert store.admit(original).code is Code.ACCEPTED
    assert store.admit(replace(original, request_key='other')).code is Code.CONFLICT
    assert counts(database) == (1, 1)


def test_outbox_failure_rolls_back_intent(database):
    with database() as conn:
        conn.execute("ALTER TABLE v2_domain_outbox ADD CONSTRAINT injected_failure "
                     "CHECK (event_type <> 'INTENT_ACCEPTED')")
    assert IntentStore(database).admit(intent()).code is Code.UNKNOWN
    assert counts(database) == (0, 0)


def test_commit_ack_loss_is_resolved_by_same_request(database):
    @contextmanager
    def lost_ack():
        with database() as conn:
            yield conn
        raise OSError('commit acknowledgement lost after commit')

    original = intent()
    assert IntentStore(lost_ack).admit(original).code is Code.UNKNOWN
    assert counts(database) == (1, 1)
    result = IntentStore(database).admit(replace(original, intent_id=str(uuid4())))
    assert result.code is Code.ALREADY_ACCEPTED
    assert result.intent_id == original.intent_id


def test_database_unavailable_is_unknown():
    def unavailable():
        raise OSError('database unavailable')

    assert IntentStore(unavailable).admit(intent()).code is Code.UNKNOWN
