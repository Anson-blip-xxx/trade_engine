from unittest.mock import patch


def test_postgres_upsert_is_noop_when_not_configured():
    from shared import postgres_client as pg

    with patch.dict('os.environ', {}, clear=False):
        with patch.object(pg, '_dsn', return_value=''):
            assert pg.upsert_trade_episode({'position_id': 'x'}) is False


def test_postgres_event_is_noop_when_not_configured():
    from shared import postgres_client as pg

    with patch.object(pg, '_dsn', return_value=''):
        assert pg.record_trade_event({'event_id': 'x'}) is False
