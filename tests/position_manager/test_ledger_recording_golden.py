"""P7-03A：record_trade ledger payload golden（income 对账 + episode upsert + CH）。"""
import pytest

from shared import trade_recorder as tr


class FakeCH:
    def __init__(self):
        self.rows = []
        self.queries = []
        self.insert_exc = None

    def query(self, sql):
        self.queries.append(sql)
        if 'count()' in sql and 'trade_history' in sql:
            return [(0,)]      # 非重复（_is_duplicate_record）
        if 'countIf' in sql:   # stats
            return [(3, 1, 100.0, 5.0, -10.0)]
        return []

    def insert(self, table, row):
        self.rows.append((table, row))
        if self.insert_exc is not None:
            raise self.insert_exc


class FakePG:
    def __init__(self):
        self.episodes = []
        self.exc = None

    def upsert(self, data):
        self.episodes.append(data)
        if self.exc is not None:
            raise self.exc
        return True


@pytest.fixture
def recorder_env(monkeypatch, fake_redis):
    ch = FakeCH()
    pg = FakePG()

    def enqueue(st):
        pass

    monkeypatch.setattr(tr, '_ch_insert', ch.insert)
    monkeypatch.setattr(tr, '_ch_query', ch.query)
    monkeypatch.setattr(tr, '_pg_upsert_trade', pg.upsert)
    monkeypatch.setattr(tr, 'enqueue_closed_trade', enqueue)
    monkeypatch.setattr(tr, '_rget', lambda k: None)
    monkeypatch.setattr(tr, '_rset', lambda k, v: None)
    monkeypatch.setattr(tr, 'fapi_get', lambda *a, **k: [])   # income 无数据
    monkeypatch.setattr(tr, 'requests', type('R', (), {
        'post': staticmethod(lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError('net off')))}), False) \
        if False else monkeypatch.setattr(
        tr, 'requests', type('Rmod', (), {
            'post': staticmethod(lambda *a, **k: None)}))
    monkeypatch.setattr(tr, 'time', type('T', (), {
        'time': staticmethod(lambda: 1_700_000_000.0)}))
    return tr, ch, pg


class TestRecordTradeEpisodePayload:
    ARGS = dict(symbol='DOGEUSDT', entry=2.0, exit_price=1.9, qty=10.0,
                leverage=3, source='S8', open_time=1_699_000_000.0,
                exit_reason='硬止损', signal_type='PULSE_DOWN',
                side='SHORT', score=70, atr_entry=0.02, sl_price=2.2,
                margin_mode='ISOLATED', position_id='S8:DOGEUSDT:2:0',
                final_close=True)

    def test_episode_upsert_full_payload(self, recorder_env):
        tr, ch, pg = recorder_env
        tr.record_trade(**self.ARGS)
        assert len(pg.episodes) == 1
        row = pg.episodes[0]
        assert row['position_id'] == 'S8:DOGEUSDT:2:0'
        assert row['symbol'] == 'DOGEUSDT'
        assert row['system_name'] == 'S8'
        assert row['side'] == 'SHORT'
        assert row['entry_price'] == 2.0
        assert row['exit_price'] == 1.9
        assert row['qty'] == 10.0
        assert row['leverage'] == 3
        assert row['exit_reason'] == '硬止损'
        assert row['event_type'] == 'PULSE_DOWN'
        assert row['strength'] == 70.0
        assert row['margin_mode'] == 'ISOLATED'
        assert row['sl_price'] == 2.2
        assert row['duration_min'] == int((1_700_000_000.0 -
                                           1_699_000_000.0) / 60)
        assert row['result'] == 'win'
        assert row['ghost_cleanup'] is False
        assert row['env'] == 'demo' or row['env'] == 'prod'

    def test_episode_pnl_formula_short_win(self, recorder_env):
        """event-tier 公式：SHORT (entry-exit)*qty = 1.0（PMB-7 公式口径冻结）。"""
        tr, ch, pg = recorder_env
        tr.record_trade(**self.ARGS)
        row = pg.episodes[0]
        assert row['pnl_usdt'] == pytest.approx(1.0)
        assert row['pnl_pct'] == pytest.approx(5.0)

    def test_ch_row_shape(self, recorder_env):
        tr, ch, pg = recorder_env
        tr.record_trade(**self.ARGS)
        assert ch.rows and ch.rows[0][0] == 'default.trade_history'
        import json
        row = json.loads(ch.rows[0][1])
        assert row['position_id'] == 'S8:DOGEUSDT:2:0'
        assert row['pnl_usdt'] == pytest.approx(1.0)
        assert row['exit_reason'] == '硬止损'

    def test_short_loss_result_label(self, recorder_env):
        tr, ch, pg = recorder_env
        tr.record_trade(**dict(self.ARGS, exit_price=2.1))
        assert pg.episodes[0]['result'] == 'loss'

    def test_long_win_formula(self, recorder_env):
        tr, ch, pg = recorder_env
        tr.record_trade(**dict(self.ARGS, side='LONG', exit_price=2.1))
        assert pg.episodes[0]['pnl_usdt'] == pytest.approx(1.0)

    def test_qty_zero_silent_skip(self, recorder_env):
        tr, ch, pg = recorder_env
        tr.record_trade(**dict(self.ARGS, qty=0))
        assert pg.episodes == [] and ch.rows == []

    def test_missing_position_id_synthesized(self, recorder_env):
        tr, ch, pg = recorder_env
        tr.record_trade(**dict(self.ARGS, position_id=''))
        # 无 position_id → f'{source}:{sym}:{entry:.12g}:{open_time:.6f}'（legacy synthesize）
        assert pg.episodes[0]['position_id'] == \
            'S8:DOGEUSDT:2:1699000000.000000'


ARGS = TestRecordTradeEpisodePayload.ARGS


class TestIncomeReconciliationPMB7:
    ARGS = ARGS
    def test_income_zero_no_reconcile_formula_used(self, recorder_env, monkeypatch):
        """income 空/异常 → 公式值直接使用（不覆盖）。"""
        tr, ch, pg = recorder_env
        tr.record_trade(**self.ARGS)
        assert pg.episodes[0]['pnl_usdt'] == pytest.approx(1.0)
        assert pg.episodes[0]['pnl_pct'] == pytest.approx(5.0)

    def test_income_real_overrides_formula(self, recorder_env, monkeypatch):
        """income 非零 → episode 用 income 数（PMB-7：两套口径各写各的）。"""
        def income_data(path, params=None):
            return [{'income': '2.5', 'symbol': 'DOGEUSDT',
                     'incomeType': 'REALIZED_PNL',
                     'time': 1_699_000_000_000}]
        tr, ch, pg = recorder_env
        tr.fapi_get = income_data
        tr.record_trade(**self.ARGS)
        assert pg.episodes[0]['pnl_usdt'] == 2.5        # income 数
        assert abs(pg.episodes[0]['pnl_pct'] - 2.5 / 20.0 * 100) < 1e-9
        # 公式值仅用于 partial 累积（无 partial 数据则被替换）

    def test_income_post_error_silent(self, recorder_env, monkeypatch):
        def income_data(path, params=None):
            raise RuntimeError('income down')
        tr, ch, pg = recorder_env
        tr.fapi_get = income_data
        tr.record_trade(**self.ARGS)
        assert pg.episodes[0]['pnl_usdt'] == pytest.approx(1.0)

    def test_income_malformed_silent(self, recorder_env, monkeypatch):
        def income_data(path, params=None):
            return ['not-dict']
        tr, ch, pg = recorder_env
        tr.fapi_get = income_data
        tr.record_trade(**self.ARGS)
        assert pg.episodes[0]['pnl_usdt'] == pytest.approx(1.0)


class TestPartialCloseLedgerPMB6:
    def test_partial_close_no_pg_no_ch(self, recorder_env, pm_full):
        """partial：record final_close=False → 仅 redis partial 累积，无 PG/CH。"""
        tr, ch, pg = recorder_env
        tr.record_trade(symbol='AUSDT', entry=2.0, exit_price=1.9,
                        qty=4.0, leverage=3, source='S8',
                        open_time=1_699_000_000.0, exit_reason='分层止盈',
                        side='SHORT', position_id='S8:AU',
                        final_close=False)
        assert pg.episodes == []                       # PMB-6 冻结
        assert ch.rows == []
        # episode partial 累积仅经 redis key `trade:partial:{sha1}`


class TestFullCloseRecording:
    def test_full_close_record_and_flush_partial(self, recorder_env, fake_redis):
        tr, ch, pg = recorder_env
        tr.record_trade(symbol='AUSDT', entry=2.0, exit_price=1.9,
                        qty=4.0, leverage=3, source='S8',
                        open_time=1_699_000_000.0, exit_reason='分层止盈',
                        side='SHORT', position_id='S8:AU',
                        final_close=False)
        tr.record_trade(symbol='AUSDT', entry=2.0, exit_price=1.9,
                        qty=6.0, leverage=3, source='S8',
                        open_time=1_699_000_000.0, exit_reason='硬止损',
                        side='SHORT', position_id='S8:AU',
                        final_close=True)
        assert len(pg.episodes) == 1                    # 合并后 upsert
        row = pg.episodes[0]
        assert abs(row['exit_price'] - 1.9) < 1e-9     # partial 加权 exit = 同价
