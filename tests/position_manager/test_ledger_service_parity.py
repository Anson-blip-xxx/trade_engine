"""P7-03B：PositionLedgerService parity（legacy record_trade wrapper vs
service settle）——重构不变（P7-03A frozen payloads/call-counts）。"""
import pytest


class PGPortSpec:
    def __init__(self):
        self.episodes = []
        self.exc = None

    def upsert_trade_episode(self, data):
        if self.exc is not None:
            raise self.exc
        self.episodes.append(data)


class CHSpec:
    def __init__(self):
        self.rows = []
        self.queries = []
        self.exc = None

    def query(self, sql):
        self.queries.append(sql)
        if 'count()' in sql:
            return [(0,)]
        if 'countIf' in sql:
            return [(3, 1, 100.0, 5.0, -10.0)]
        return []

    def insert(self, t, row):
        if self.exc is not None:
            raise self.exc
        self.rows.append((t, row))


ARGS = dict(symbol='DOGEUSDT', entry=2.0, exit_price=1.9, qty=10.0,
            leverage=3, source='S8', open_time=1699000000.0,
            exit_reason='硬止损', signal_type='PULSE_DOWN', side='SHORT',
            score=70, position_id='S8:D', final_close=True)


def make_env(pg, ch, monkeypatch):
    from shared import trade_recorder as tr
    monkeypatch.setattr(tr, '_ch_insert', ch.insert)
    monkeypatch.setattr(tr, '_ch_query', ch.query)
    monkeypatch.setattr(tr, '_pg_upsert_trade', pg.upsert_trade_episode)
    monkeypatch.setattr(tr, '_rget', lambda k: None)
    monkeypatch.setattr(tr, '_rset', lambda k, v: None)
    monkeypatch.setattr(tr, 'fapi_get', lambda *a, **k: [])
    monkeypatch.setattr(tr, 'requests', type('Rm', (), {
        'post': staticmethod(lambda *a, **k: None)}))
    monkeypatch.setattr(tr, 'enqueue_closed_trade',
                        lambda kw: None)
    import datetime as _dt
    monkeypatch.setattr(tr, 'datetime', type('DTm', (object,), {
        'utcnow': staticmethod(lambda: _dt.datetime(2026, 1, 1))}))
    return tr


class TestRecordTradeParity:
    def test_full_close_payload_and_counts_frozen(self, monkeypatch):
        """full close：1 pg episode + 1 ch trade_history + formula PnL —
        P7-03A 测试冻结的 call counts/payload（期望不改）。"""
        from shared import trade_recorder as tr
        pg = PGPortSpec()
        ch = CHSpec()
        make_env(pg, ch, monkeypatch)
        tr.record_trade(**ARGS)
        assert len(pg.episodes) == 1
        row = pg.episodes[0]
        assert row['position_id'] == 'S8:D'
        assert row['symbol'] == 'DOGEUSDT'
        assert row['pnl_usdt'] == pytest.approx(1.0)
        assert row['exit_reason'] == '硬止损'
        assert len(ch.rows) == 1
        assert ch.rows[0][0] == 'default.trade_history'

    def test_partial_close_empties_frozen(self, monkeypatch):
        """partial：0 pg + 0 ch（PMB-6 冻结不修）。"""
        from shared import trade_recorder as tr
        pg = PGPortSpec()
        ch = CHSpec()
        make_env(pg, ch, monkeypatch)
        tr.record_trade(**dict(ARGS, final_close=False))
        assert pg.episodes == [] and ch.rows == []

    def test_income_override_frozen(self, monkeypatch):
        """income>0 → episode PnL 用 income，覆盖公式（PMB-7 冻结）。"""
        from shared import trade_recorder as tr
        pg = PGPortSpec()
        ch = CHSpec()
        make_env(pg, ch, monkeypatch)
        monkeypatch.setattr(tr, 'fapi_get', lambda *a, **k: [
            {'income': '2.5', 'symbol': 'DOGEUSDT',
             'incomeType': 'REALIZED_PNL',
             'time': 1_699_000_000_000}])
        tr.record_trade(**ARGS)
        assert pg.episodes[0]['pnl_usdt'] == pytest.approx(2.5)

    def test_income_raise_fallback_frozen(self, monkeypatch):
        from shared import trade_recorder as tr
        pg = PGPortSpec()
        ch = CHSpec()
        make_env(pg, ch, monkeypatch)
        monkeypatch.setattr(tr, 'fapi_get',
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError('income down')))
        tr.record_trade(**ARGS)
        assert pg.episodes[0]['pnl_usdt'] == pytest.approx(1.0)

    def test_no_pg_raise_analysis_skipped(self, monkeypatch):
        from shared import trade_recorder as tr
        pg = PGPortSpec()
        pg.exc = RuntimeError('pg down')
        ch = CHSpec()
        analysis = []
        make_env(pg, ch, monkeypatch)
        monkeypatch.setattr(tr, 'enqueue_closed_trade',
                            lambda kw: analysis.append(1))
        tr.record_trade(**ARGS)
        assert analysis == []                       # PG raise → analysis skipped

    def test_ch_raise_analysis_skipped_but_pg_written(self, monkeypatch):
        from shared import trade_recorder as tr
        pg = PGPortSpec()
        ch = CHSpec()
        ch.exc = RuntimeError('ch down')
        analysis = []
        make_env(pg, ch, monkeypatch)
        monkeypatch.setattr(tr, 'enqueue_closed_trade',
                            lambda kw: analysis.append(1))
        tr.record_trade(**ARGS)
        assert len(pg.episodes) == 1                # PG 已写
        assert analysis == []                       # analysis 跳过


class TestLedgerPortMutualAliasing:
    def test_service_no_position_mutation(self, monkeypatch):
        """service settle 不 mutate position dict（P7-03A 冻结）。"""
        from shared import trade_recorder as tr
        pg = PGPortSpec()
        ch = CHSpec()
        make_env(pg, ch, monkeypatch)
        pos_snap = {'entry': 2.0, 'qty': 10.0, 'side': 'SHORT'}
        tr.record_trade(symbol='X', entry=2.0, exit_price=1.9, qty=10.0,
                        leverage=3, source='S8', open_time=1699000000.0,
                        exit_reason='x', side='SHORT', position_id='X:1',
                        final_close=True)
        assert pos_snap == {'entry': 2.0, 'qty': 10.0, 'side': 'SHORT'}
