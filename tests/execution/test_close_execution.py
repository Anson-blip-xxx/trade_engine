"""Golden：PM._close / PM._partial_close Execution 行为。"""
import time

from shared import position_manager as pm


def _pos(**kw):
    base = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
            'system': 'S8', 'open_time': time.time(), 'leverage': 3,
            'sl': 2.2, 'signal_type': 'TREND_DOWN', 'event_type': 'TREND_DOWN',
            'score': 66, 'be_done': False}
    base.update(kw)
    return base


# ── Close Execution ─────────────────────────────────────────────────────

class TestCloseExecution:
    def test_full_close_short_success(self, close_env):
        """正常平空：MARKET BUY reduceOnly → record → PG → 移除。"""
        pos = _pos()
        positions = {'AUSDT': pos}
        r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
        assert r is True
        assert 'AUSDT' not in positions

        order = [p for p in close_env['calls']['post'] if 'order' in p['path']][0]
        assert order['params']['side'] == 'BUY'
        assert order['params']['type'] == 'MARKET'
        assert order['params']['reduceOnly'] == 'true'
        assert order['params']['positionSide'] == 'BOTH'
        assert order['params']['quantity'] == 10.0

        rec = close_env['calls']['record'][0]
        assert rec['kwargs']['side'] == 'SHORT'
        assert rec['kwargs']['exit_reason'] == '硬止损'
        assert rec['kwargs']['final_close'] is True

        pg_types = [e['event_type'] for e in close_env['calls']['pg']]
        assert 'CLOSE_ORDER_FILLED' in pg_types

    def test_full_close_long_side_sell(self, close_env):
        """平多 → SELL。"""
        pos = _pos(side='LONG')
        positions = {'AUSDT': pos}
        close_env['set_risk_responses']([[{'symbol': 'AUSDT', 'positionAmt': '10', 'entryPrice': '2.0'}]])
        r = close_env['pm']._close('AUSDT', pos, 2.1, '手动平仓', positions)
        assert r is True
        order = [p for p in close_env['calls']['post'] if 'order' in p['path']][0]
        assert order['params']['side'] == 'SELL'

    def test_close_exchange_already_flat(self, close_env):
        """交易所已无持仓 → 记录 + EXCHANGE_POSITION_FLAT + 移除。"""
        pos = _pos()
        positions = {'AUSDT': pos}
        close_env['set_risk_responses']([])  # 无持仓
        r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
        assert r is True
        assert 'AUSDT' not in positions
        pg_types = [e['event_type'] for e in close_env['calls']['pg']]
        assert 'EXCHANGE_POSITION_FLAT' in pg_types
        # 无 order POST（交易所已平，无需下单）
        order_posts = [p for p in close_env['calls']['post'] if 'order' in p['path']]
        assert order_posts == []

    def test_close_rejected_clears_marker(self, close_env):
        """交易所拒绝 → 清除标记 + False + 持仓保留。"""
        pos = _pos()
        positions = {'AUSDT': pos}
        close_env['set_order_result']({'code': -2019, 'msg': 'Margin is insufficient.'})
        r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
        assert r is False
        assert 'AUSDT' in positions
        assert close_env['redis'].get('closed:AUSDT') is None

    def test_close_partial_fill_keeps_remaining(self, close_env):
        """部分成交：记录 final_close=False，保留剩余 qty。"""
        pos = _pos(qty=10.0, original_qty=10.0)
        positions = {'AUSDT': pos}
        close_env['set_risk_responses']([
            [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}],   # 第一次查仓
            [{'symbol': 'AUSDT', 'positionAmt': '-6', 'entryPrice': '2.0'}],    # 平仓后剩 6
        ])
        close_env['set_order_result'](
            {'orderId': 1, 'status': 'FILLED', 'executedQty': '4'})
        r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
        assert r is False
        assert positions['AUSDT']['qty'] == 6.0
        rec = close_env['calls']['record'][0]
        assert rec['kwargs']['final_close'] is False

    def _make_spies(self, close_env, monkeypatch, events):
        """对 _close 全部关键协作点做 spy，按真实发生顺序记录。"""
        pm = close_env['pm']
        raw_get = close_env['fapi_get']
        raw_post = close_env['fapi_post']
        risk_n = {'n': 0}

        def spy_get(path, params=None):
            r = raw_get(path, params)
            if 'positionRisk' in path:
                risk_n['n'] += 1
                events.append(f'positionRisk#{risk_n["n"]}')
            return r

        def spy_post(path, params=None):
            r = raw_post(path, params)
            if 'order' in path:
                events.append(('order', dict(params)))
            return r

        def spy_record(*a, **kw):
            events.append(('record_trade', kw.get('final_close')))

        def spy_pg(event):
            events.append(('pg_record', event['event_type']))

        monkeypatch.setattr(pm, '_s6api', lambda: (
            spy_get, spy_post, lambda *a, **k: {},
            lambda sym: 2.0, lambda sym: (6, 6),
            lambda *a, **k: (0, 0, 0), lambda *a, **k: 50.0, spy_record))
        monkeypatch.setattr(pm, '_pg_record_event', spy_pg)
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda sym: events.append('cancel_algo'))
        monkeypatch.setattr(pm, '_mark_closed', lambda sym: events.append('mark_closed'))
        monkeypatch.setattr(pm, '_save', lambda positions: events.append('save'))

    def test_close_full_call_sequence(self, close_env, monkeypatch):
        """完整成交平仓调用顺序（spy 捕获真实顺序）：
        mark → positionRisk#1 → order → positionRisk#2 → cancel_algo
        → pg_record(CLOSE_ORDER_FILLED) → record_trade(final=True) → save"""
        pos = _pos()
        positions = {'AUSDT': pos}
        close_env['set_risk_responses']([
            [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}], []])
        events = []
        self._make_spies(close_env, monkeypatch, events)

        r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
        assert r is True
        assert events == [
            'mark_closed',
            'positionRisk#1',
            ('order', {'symbol': 'AUSDT', 'side': 'BUY', 'type': 'MARKET',
                       'quantity': 10.0, 'positionSide': 'BOTH',
                       'reduceOnly': 'true'}),
            'positionRisk#2',
            'cancel_algo',
            ('pg_record', 'CLOSE_ORDER_FILLED'),
            ('record_trade', True),
            'save',
        ]

    def test_close_flat_path_call_sequence(self, close_env, monkeypatch):
        """交易所已平仓分支顺序（与 full-close 分支不同）：
        mark → positionRisk#1 → cancel_algo → record_trade(final=True)
        → pg_record(EXCHANGE_POSITION_FLAT) → save
        差异：无 order / 无 positionRisk#2；record_trade 在 pg_record 之前。"""
        pos = _pos()
        positions = {'AUSDT': pos}
        close_env['set_risk_responses']([[]])   # 首次查仓即空
        events = []
        self._make_spies(close_env, monkeypatch, events)

        r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
        assert r is True
        assert events == [
            'mark_closed',
            'positionRisk#1',
            'cancel_algo',
            ('record_trade', True),
            ('pg_record', 'EXCHANGE_POSITION_FLAT'),
            'save',
        ]


# ── Partial Close Execution ─────────────────────────────────────────────

class TestPartialCloseExecution:
    def test_long_partial(self, close_env):
        """LONG 分层止盈：qty 减少，pnl 正确，无 record_trade。"""
        pos = _pos(side='LONG', entry=1.0, qty=10.0, original_qty=10.0)
        positions = {'AUSDT': pos}
        close_env['pm']._partial_close('AUSDT', pos, 1.1, 5.0, 5, positions)
        assert pos['qty'] == 5.0
        assert len(close_env['calls']['record']) == 0

    def test_short_partial(self, close_env):
        pos = _pos(side='SHORT', entry=2.0, qty=10.0, original_qty=10.0)
        positions = {'AUSDT': pos}
        close_env['pm']._partial_close('AUSDT', pos, 1.9, 4.0, 5, positions)
        assert pos['qty'] == 6.0

    def test_partial_close_order_params(self, close_env):
        """部分平仓订单参数：SHORT → BUY，positionSide=BOTH，无 reduceOnly（当前行为）。"""
        pos = _pos(side='SHORT', qty=10.0)
        positions = {'AUSDT': pos}
        close_env['pm']._partial_close('AUSDT', pos, 1.9, 4.0, 5, positions)
        order = [p for p in close_env['calls']['post'] if 'order' in p['path']][0]
        assert order['params']['side'] == 'BUY'
        assert order['params']['type'] == 'MARKET'
        assert order['params']['positionSide'] == 'BOTH'
        assert 'reduceOnly' not in order['params']  # OBS：_close 有，_partial_close 无

    def test_partial_close_rejected_no_change(self, close_env):
        """交易所拒绝 → qty 不变。"""
        pos = _pos(qty=10.0)
        positions = {'AUSDT': pos}
        close_env['set_order_result']({'code': -1111, 'msg': 'Precision error'})
        close_env['pm']._partial_close('AUSDT', pos, 1.1, 5.0, 5, positions)
        assert pos['qty'] == 10.0   # 不变

    def test_partial_close_negative_qty_increases(self, close_env):
        """Observed Current Behavior：close_qty<0 → qty 增加（10-(-100)=110）。"""
        pos = _pos(qty=10.0)
        positions = {'AUSDT': pos}
        close_env['pm']._partial_close('AUSDT', pos, 1.1, -100.0, 5, positions)
        assert pos['qty'] == 110.0   # 10 - (-100) = 110
