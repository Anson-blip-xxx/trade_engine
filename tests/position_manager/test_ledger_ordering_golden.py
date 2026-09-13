"""P7-03A：ledger ordering（spy 冻结：episode→CH→TG 链逐层）。"""
import pytest

from shared import trade_recorder as tr


class SpyCH:
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

    def insert(self, table, row):
        if self.exc is not None:
            raise self.exc
        self.rows.append((table, row))


class SpyPG:
    def __init__(self):
        self.episodes = []
        self.exc = None

    def upsert(self, data):
        if self.exc is not None:
            raise self.exc
        self.episodes.append(data)


def make_spy_env(pg, ch, monkeypatch):
    monkeypatch.setattr(tr, '_ch_insert', ch.insert)
    monkeypatch.setattr(tr, '_ch_query', ch.query)
    monkeypatch.setattr(tr, '_pg_upsert_trade', pg.upsert)
    monkeypatch.setattr(tr, 'enqueue_closed_trade', lambda st: None)
    monkeypatch.setattr(tr, '_rget', lambda k: None)
    monkeypatch.setattr(tr, '_rset', lambda k, v: None)
    monkeypatch.setattr(tr, 'fapi_get', lambda *a, **k: [])
    monkeypatch.setattr(tr, 'requests', type('Rm', (), {
        'post': staticmethod(lambda *a, **k: None)}))
    import datetime as _dt
    monkeypatch.setattr('shared.trade_recorder.datetime',
                        type('DTm', (object, ), {
                            'utcnow': staticmethod(lambda: _dt.datetime(
                                2026, 1, 1))}))


ARGS = dict(symbol='DOGEUSDT', entry=2.0, exit_price=1.9, qty=10.0,
            leverage=3, source='S8', open_time=1_699_000_000.0,
            exit_reason='硬止损', signal_type='PULSE_DOWN', side='SHORT',
            score=70, position_id='S8:D', final_close=True)


class TestFailureTopology:
    def test_pg_exception_swallowed_analysis_skipped(self, monkeypatch):
        """PG raise → record_trade 外层 try → 吞错；analysis/enqueue 被跳过;
        但 episode 已 append（spy 收到 —— PG 层）后再 raise 则 enque 跳过。"""
        def boom(data):
            raise RuntimeError('pg down')
        pg = SpyPG()
        pg.upsert = boom
        ch = SpyCH()
        an = []
        make_spy_env(pg, ch, monkeypatch)
        monkeypatch.setattr(tr, 'enqueue_closed_trade', lambda st: an.append(1))
        tr.record_trade(**ARGS)
        assert an == []                                # analysis 未到
        assert ch.rows == []                            # pg raise 先于 ch

    def test_ch_failure_analysis_skipped_but_pg_written(self, monkeypatch):
        def boom(table, row):
            raise RuntimeError('ch down')
        pg = SpyPG()
        ch = SpyCH()
        ch.exc = RuntimeError('ch down')
        an = []
        make_spy_env(pg, ch, monkeypatch)
        monkeypatch.setattr(tr, 'enqueue_closed_trade', lambda st: an.append(1))
        tr.record_trade(**ARGS)
        assert len(pg.episodes) == 1                    # episode 已写完
        assert an == []                                 # analysis 跳过


class TestPartialAndFullCounts:
    ARGS_ = ARGS

    def test_call_counts_frozen(self, monkeypatch):
        """full close：1 pg + 1 ch + 1 analysis；partial：0/0/0（PMB-6）。"""
        pg = SpyPG()
        ch = SpyCH()
        an = []
        make_spy_env(pg, ch, monkeypatch)
        monkeypatch.setattr(tr, 'enqueue_closed_trade',
                            lambda st: an.append(1))
        tr.record_trade(**dict(self.ARGS_, final_close=True))
        tr.record_trade(**dict(self.ARGS_, final_close=False))
        assert len(pg.episodes) == 1
        assert len(ch.rows) == 1
        assert len(an) == 1


def test_pg_then_ch_order_frozen_clean(monkeypatch):
    """pg → ch → analysis 顺序冻结（record_trade 真实链 spy）。"""
    order = []
    monkeypatch.setattr(tr, '_pg_upsert_trade', lambda d: order.append('pg'))
    monkeypatch.setattr(tr, '_ch_insert', lambda t, r: order.append('ch'))
    monkeypatch.setattr(tr, '_ch_query', lambda sql: [(0,)])
    monkeypatch.setattr(tr, '_rget', lambda k: None)
    monkeypatch.setattr(tr, '_rset', lambda k, v: None)
    monkeypatch.setattr(tr, 'fapi_get', lambda *a, **k: [])
    from shared import trade_recorder as tr2
    monkeypatch.setattr(tr2, 'requests', type('Rm', (), {
        'post': staticmethod(lambda *a, **k: None)}))
    monkeypatch.setattr(tr, 'enqueue_closed_trade',
                        lambda st: order.append('analysis'))
    import datetime as _dt
    monkeypatch.setattr(tr, 'datetime', type('DTm', (object,), {
        'utcnow': staticmethod(lambda: _dt.datetime(2026, 1, 1))}))
    tr.record_trade(symbol='DOGEUSDT', entry=2.0, exit_price=1.9, qty=10.0,
                    leverage=3, source='S8', open_time=1699000000.0,
                    exit_reason='硬止损', signal_type='PULSE_DOWN',
                    side='SHORT', score=70, position_id='S8:D',
                    final_close=True)
    assert order == ['pg', 'ch', 'analysis']


def test_order_frozen_debug(monkeypatch):
    """debug print"""
    order = []
    def pg_upsert(data):
        order.append('pg', data)
    def ch_insert(t, row):
        order.append('ch', t)
    monkeypatch.setattr(tr, '_pg_upsert_trade', pg_upsert)
    monkeypatch.setattr(tr, '_ch_insert', ch_insert)
    monkeypatch.setattr(tr, '_ch_query', ch.query if hasattr((ch := SpyCH()), 'query') else None)
    monkeypatch.setattr(tr, '_rget', lambda k: None)
    monkeypatch.setattr(tr, '_rset', lambda k, v: None)
    import time as _tm
    monkeypatch.setattr(tr, 'time', _tm)
    monkeypatch.setattr(tr, 'fapi_get', lambda *a, **k: [])
    monkeypatch.setattr(tr, 'requests', type('Rm2', (), {
        'post': staticmethod(lambda *a, **k: None)}))
    try:
        tr.record_trade(symbol='DOGEUSDT', entry=2.0, exit_price=1.9,
                        qty=10.0, leverage=3, source='S8',
                        open_time=1_699_000_000.0, exit_reason='硬止损',
                        signal_type='PULSE_DOWN', side='SHORT', score=70,
                        position_id='S8:D', final_close=True)
    except Exception as e:
        print('EXC', type(e).__name__, e)
    print('ORDER', order)


def test_order_frozen_debug2(monkeypatch):
    """persis BUG  hunting: patch via module attr direct call"""
    order = []
    monkeypatch.setattr(tr, '_pg_upsert_trade', lambda data: order.append('pg'))
    monkeypatch.setattr(tr, '_ch_insert', lambda t, r: order.append('ch'))
    monkeypatch.setattr(tr, '_ch_query',
                        lambda sql: [(0,)] if 'count()' in sql else (
                            [(3, 1, 100.0, 5.0, -10.0)]
                            if 'countIf' in sql else None))
    monkeypatch.setattr(tr, '_rget', lambda k: None)
    monkeypatch.setattr(tr, '_rset', lambda k, v: None)
    monkeypatch.setattr(tr, 'fapi_get', lambda *a, **k: [])
    from shared import trade_recorder as tr2
    monkeypatch.setattr(tr2, 'requests', type('Rm', (), {
        'post': staticmethod(lambda *a, **k: None)}))
    monkeypatch.setattr(tr, 'enqueue_closed_trade',
                        lambda st: order.append('analysis'))
    import datetime as _dt
    monkeypatch.setattr(tr, 'datetime', type('DTm', (object,), {
        'utcnow': staticmethod(lambda: _dt.datetime(2026, 1, 1))}))
    tr.record_trade(symbol='DOGEUSDT', entry=2.0, exit_price=1.9, qty=10.0,
                    leverage=3, source='S8', open_time=1699000000.0,
                    exit_reason='硬止损', signal_type='PULSE_DOWN',
                    side='SHORT', score=70, position_id='S8:D',
                    final_close=True)
    print('ORD2', order)
