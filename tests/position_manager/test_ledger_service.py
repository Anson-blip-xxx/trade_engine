"""P7-03B：PositionLedgerService contract tests（service 面单独覆盖）。"""
import pytest

from position_ledger.service import PositionLedgerService


def make_svc(record=None, ch_query=None):
    return PositionLedgerService(
        ledger_port=type('P', (), {'upsert_trade_episode': lambda s, d: None}),
        duplicate_check_fn=lambda *a: False,
        partial_key_fn=lambda pid: 'trade:partial:xyz',
        income_fetch_fn=lambda *a, **k: None,
        redis_get=lambda k: None,
        redis_set=lambda k, v: None,
        loss_cooldown_fn=lambda s: None,
        ch_query_fn=lambda sql: ch_query(sql) if ch_query else [(0,)],
        ch_insert_fn=lambda t, r: None,
        analysis_fn=lambda kw: None,
        env_fn=lambda: 'demo',
        tg_fn=lambda msg: None,
        time_fn=lambda: 1_700_000_000.0)


class TestServiceContract:
    def test_service_creation_minimal(self):
        svc = make_svc()
        assert svc is not None

    def test_api_surface(self):
        import inspect
        methods = [name for name, _ in inspect.getmembers(
            PositionLedgerService, predicate=inspect.isfunction)
            if not name.startswith('__')]
        assert 'settle' in methods

    def test_port_reuse_via_shim(self):
        """PositionLedgerService 复用既有 PositionLedgerPort 语义；
        wrapper 经 _PortShim 注入（P4-D3 Port reuse）；P7-03A parity tests
        已覆盖，本例为 contract 面验证。"""
        episodes = []
        port_shim = type('PortShim', (), {
            'upsert_trade_episode': lambda self_p, data: episodes.append(data)
        })()
        svc = PositionLedgerService(
            ledger_port=port_shim,
            duplicate_check_fn=lambda *a: False,
            partial_key_fn=lambda pid: 'trade:partial:xyz',
            income_fetch_fn=lambda *a, **k: None,
            redis_get=lambda k: None,
            redis_set=lambda k, v: None,
            loss_cooldown_fn=lambda s: None,
            ch_query_fn=lambda sql: (3, 1, 100.0, 5.0, -10.0),
            ch_insert_fn=lambda t, r: None,
            analysis_fn=lambda kw: None,
            env_fn=lambda: 'demo',
            tg_fn=lambda msg: None,
            time_fn=lambda: 1_700_000_000.0)
        svc.settle(symbol='DOGEUSDT', entry=2.0, exit_price=1.9, qty=10.0,
                   leverage=3, source='S8', open_time=1699000000.0,
                   exit_reason='硬止损', signal_type='PULSE_DOWN',
                   side='SHORT', score=70, position_id='S8:D',
                   final_close=True)
        assert len(episodes) == 1                 # 经 Port upsert 成功
        assert episodes[0]['position_id'] == 'S8:D'
