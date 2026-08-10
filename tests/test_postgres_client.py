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


def test_performance_metrics_summarize_position_rows():
    from shared.performance_metrics import summarize_episodes

    result = summarize_episodes([
        {'pnl_usdt': 3, 'exit_reason': 'take_profit'},
        {'pnl_usdt': -1, 'exit_reason': 'stop_loss'},
    ])

    assert result['trades'] == 2
    assert result['wins'] == 1
    assert result['pnl_usdt'] == 2
    assert result['profit_factor'] == 3
