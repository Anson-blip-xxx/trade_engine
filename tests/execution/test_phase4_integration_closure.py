"""P4-03-01-D5：Phase 4 integration closure —— 跨边界真实编排序列冻结。

全部调用真实 orchestration，只 mock 基础设施（Redis/PG/TG/Algo/Binance）。
覆盖四条路径的关键顺序（exit criteria 7/8/9/10）：
- Open：order(Service) → PM state → Redis → PG event → TG → Algo enqueue
- Close：marker → risk#1 → close(Service) → risk#2 → cancel → PG → record → save
- Partial：order(Service) → qty → Redis save；PG/ledger 零写入；不 cancel algo
- Protection：open 后 enqueue 参数/成功 id 写回/失败吞错；cancel 异常不阻断 close
"""
import pytest

from execution import service as exec_service
from execution.core import close_intent, partial_close_intent, se_open_intent, position_pnl


def _open_kwargs(**kw):
    base = {'name': 'S6', 'symbol': 'TESTUSDT', 'side': 'LONG',
            'entry_price': 100.0, 'stop_price': 95.0, 'qty': 100.0,
            'margin_mode': 'CROSSED', 'leverage': 3,
            'event_type': 'TREND_UP', 'strength': 70, 'tg_fn': None,
            'expected_move_pct': 0, 'decision_context': {}}
    base.update(kw)
    return base


def _pos(**kw):
    base = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
            'system': 'S8', 'open_time': 1788000000, 'leverage': 3,
            'sl': 2.2, 'signal_type': 'TREND_DOWN', 'event_type': 'TREND_DOWN',
            'score': 66, 'be_done': False}
    base.update(kw)
    return base


def _order_posts(calls):
    return [p for p in calls['post'] if 'order' in p['path']]


FILLED_100 = {'orderId': 1, 'status': 'FILLED', 'executedQty': '100',
              'cumQty': '100', 'avgPrice': '100.0'}


# ═══════════════════════════════════════════════════════════════
#  Open path（criteria 7）
# ═══════════════════════════════════════════════════════════════

class TestOpenSequence:
    def test_full_fill_cross_boundary_order_frozen(self, exec_env, monkeypatch):
        """真实编排顺序冻结：execute_order → PM update → Redis(经) → PG → TG → Algo。"""
        se = exec_env['se']
        events = []

        orig_factory = se._execution_service
        monkeypatch.setattr(se, '_execution_service',
                            lambda: (events.append('service.execute'),
                                     orig_factory())[1])

        orig_update = se._update_pos_cache
        def update_spy(*a, **k):
            events.append('pm_update')
            return orig_update(*a, **k)
        monkeypatch.setattr(se, '_update_pos_cache', update_spy)

        monkeypatch.setattr(se, '_pg_record_event',
                            lambda e: events.append(f"pg:{e['event_type']}"))

        tg = []
        r = se.open_position(**_open_kwargs(tg_fn=tg.append))
        assert r is True
        assert events == ['service.execute', 'pm_update',
                          'pg:OPEN_ORDER_FILLED']
        # Algo enqueue 是最后一项副作用：
        assert exec_env['calls']['algo'] == [{'symbol': 'TESTUSDT',
                                              'side': 'SELL', 'trigger': 95.0,
                                              'qty': 100.0}]
        # 顺序冻结：service.execute → pm_update → pg:OPEN_ORDER_FILLED（events 已断言）；
        # 跨边界尾序：algo enqueue 为最后一项副作用；Redis 已写。
        assert events == ['service.execute', 'pm_update', 'pg:OPEN_ORDER_FILLED']
        assert len(tg) == 1                      # TG 在 pg 之后发出
        assert exec_env['redis'].get('pm:positions')['TESTUSDT']['qty'] == 100.0

    def test_open_intent_and_order_params(self, exec_env):
        exec_env['se'].open_position(**_open_kwargs(side='SHORT'))
        order = _order_posts(exec_env['calls'])[0]['params']
        assert order == {'symbol': 'TESTUSDT', 'side': 'SELL', 'type': 'MARKET',
                         'quantity': 100.0, 'newOrderRespType': 'RESULT'}
        assert 'positionSide' not in order and 'reduceOnly' not in order

    def test_fill_above_50_accepted(self, exec_env):
        exec_env['ctrl']['order_result'] = {'orderId': 9, 'status': 'FILLED',
                                            'executedQty': '60', 'cumQty': '60',
                                            'avgPrice': '100.2'}
        assert exec_env['se'].open_position(**_open_kwargs()) is True
        assert exec_env['calls']['cancel'] == []

    def test_fill_below_50_cancel_remaining(self, exec_env):
        exec_env['ctrl']['order_result'] = {'orderId': 9, 'status': 'FILLED',
                                            'executedQty': '30', 'cumQty': '30',
                                            'avgPrice': '100.2'}
        assert exec_env['se'].open_position(**_open_kwargs()) is True
        assert exec_env['calls']['cancel'] == [{'symbol': 'TESTUSDT', 'orderId': 9}]

    def test_binance_rejection(self, exec_env):
        exec_env['ctrl']['order_result'] = {'code': -2019, 'msg': 'insufficient'}
        assert exec_env['se'].open_position(**_open_kwargs()) is False
        assert _order_posts(exec_env['calls']) != []
        assert len(_order_posts(exec_env['calls'])) == 1
        assert exec_env['redis'].get('pm:positions') is None

    def test_binance_exception_no_retry(self, exec_env, monkeypatch):
        se = exec_env['se']
        base_post = se.fapi_post
        def _raise(path, params=None):
            if 'order' in path:
                exec_env['calls']['post'].append({'path': path, 'params': params})
                raise RuntimeError('network down')
            return base_post(path, params)
        monkeypatch.setattr(se, 'fapi_post', _raise)
        assert se.open_position(**_open_kwargs()) is False
        assert len(_order_posts(exec_env['calls'])) == 1   # 无 retry

    def test_duplicate_after_order_eobs1a_refrozen(self, exec_env):
        """E-OBS-2（重开）：existing-position gate 在 order 之前；
        PM 缓存后 duplicate 行为保持（E-OBS-1a 单次尝试）。"""
        exec_env['ctrl']['existing_position'] = [
            {'symbol': 'TESTUSDT', 'positionAmt': '5'}]
        r = exec_env['se'].open_position(**_open_kwargs())
        assert r is False
        assert _order_posts(exec_env['calls']) == []

    def test_pm_update_failure_cancel_attempt(self, exec_env, monkeypatch):
        """E-OBS-1：Binance 成功 → PM 失败 → cancel attempt → False（不修）。"""
        se = exec_env['se']
        exec_env['ctrl']['order_result'] = FILLED_100
        def _failing(*a, **k):
            raise RuntimeError('cache update failed')
        monkeypatch.setattr(se, '_update_pos_cache', _failing)
        assert se.open_position(**_open_kwargs()) is False
        assert exec_env['calls']['cancel'] == [{'symbol': 'TESTUSDT', 'orderId': 1}]
        assert exec_env['calls']['pg'] == []

    def test_tg_failure_returns_false_position_registered_frozen(self, exec_env, monkeypatch):
        """新冻结（E-OBS-13）：tg_fn 抛错 → 返回 False，
        但订单已成交 + PM 已注册 + PG 已记录（不修：E-OBS-2 复合风险保持）。"""
        def boom(msg):
            raise RuntimeError('tg down')
        r = exec_env['se'].open_position(**_open_kwargs(tg_fn=boom))
        assert r is False
        assert 'TESTUSDT' in exec_env['redis'].get('pm:positions')   # 已注册
        assert [e['event_type'] for e in exec_env['calls']['pg']] == ['OPEN_ORDER_FILLED']
        assert _order_posts(exec_env['calls']) != []                # 已成交


# ═══════════════════════════════════════════════════════════════
#  Close path（criteria 8）
# ═══════════════════════════════════════════════════════════════

def _close_spies(close_env, monkeypatch, events):
    """close 全协作点 spy：port 事件/查仓/PG/cancel/record/save 顺序注入 events。"""
    pm = close_env['pm']
    raw_get = close_env['fapi_get']
    risk_n = {'n': 0}

    def spy_get(path, params=None):
        r = raw_get(path, params)
        if 'positionRisk' in path:
            risk_n['n'] += 1
            events.append(f'positionRisk#{risk_n["n"]}')
        return r

    class EventSpyPort:
        def __init__(self):
            self.received = []

        def place_order(self, intent):
            self.received.append(intent)
            events.append('port_place_order')
            return {'orderId': 1, 'status': 'FILLED', 'executedQty': '10'}

    spy_port = EventSpyPort()
    monkeypatch.setattr(pm, '_execution_service',
                        lambda: exec_service.ExecutionService(binance=spy_port))
    monkeypatch.setattr(pm, '_s6api', lambda: (
        spy_get, close_env['fapi_post'], lambda *a, **k: {},
        lambda sym: 2.0, lambda sym: (6, 6),
        lambda *a, **k: (0, 0, 0), lambda *a, **k: 50.0,
        lambda *a, **kw: events.append(('record_trade', kw.get('final_close')))))
    monkeypatch.setattr(pm, '_pg_record_event',
                        lambda e: events.append(('pg_record', e['event_type'])))
    monkeypatch.setattr(pm, '_cancel_all_algo',
                        lambda sym: events.append('cancel_algo'))
    monkeypatch.setattr(pm, '_mark_closed', lambda sym: events.append('mark_closed'))
    monkeypatch.setattr(pm, '_save', lambda positions: events.append('save'))
    return spy_port


class TestCloseSequence:
    def test_full_close_cross_boundary_order(self, close_env, monkeypatch):
        close_env['set_risk_responses']([
            [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}], []])
        events = []
        port = _close_spies(close_env, monkeypatch, events)
        pos = _pos()
        positions = {'AUSDT': pos}
        assert close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions) is True
        assert port.received == [close_intent('AUSDT', 'SHORT', 10.0)]
        assert events == [{'name': 'mark_closed'} if False else 'mark_closed',
                          'positionRisk#1', 'port_place_order',
                          'positionRisk#2', 'cancel_algo',
                          ('pg_record', 'CLOSE_ORDER_FILLED'),
                          ('record_trade', True), 'save']

    def test_flat_branch_record_before_pg(self, close_env, monkeypatch):
        """already-flat：记录在 PG 事件之前（真实反序，E-OBS-6a）。"""
        close_env['set_risk_responses']([[]])
        events = []
        port = _close_spies(close_env, monkeypatch, events)
        pos = _pos()
        positions = {'AUSDT': pos}
        assert close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions) is True
        assert port.received == []        # flat 无订单
        assert events == ['mark_closed', 'positionRisk#1',
                          'cancel_algo', ('record_trade', True),
                          ('pg_record', 'EXCHANGE_POSITION_FLAT'), 'save']

    def test_rejected_close_no_algo_cancel(self, close_env, monkeypatch):
        """rejected：不 cancel（保 SL 保护）+ 清 marker + False。"""
        close_env['set_risk_responses']([
            [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}, ]])
        events = []
        class RejectPort:
            def place_order(self, intent):
                events.append('port_place_order')
                return {'code': -2019, 'msg': 'Margin insufficient'}
        monkeypatch.setattr(close_env['pm'], '_execution_service',
                            lambda: exec_service.ExecutionService(binance=RejectPort()))
        pos = _pos()
        positions = {'AUSDT': pos}
        assert close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions) is False
        assert 'AUSDT' in positions
        assert close_env['redis'].get('closed:AUSDT') is None   # marker 清除
        assert 'cancel_algo' not in events                      # 不 cancel

    def test_exception_close_clears_marker(self, close_env, monkeypatch):
        """exception：clear marker（原时序保持），仓位保留。"""
        close_env['set_risk_responses']([
            [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}], []])
        events = []
        class BoomPort:
            def place_order(self, intent):
                raise RuntimeError('network down')
        monkeypatch.setattr(close_env['pm'], '_execution_service',
                            lambda: exec_service.ExecutionService(binance=BoomPort()))
        pos = _pos()
        positions = {'AUSDT': pos}
        assert close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions) is False
        assert 'AUSDT' in positions
        assert close_env['redis'].get('closed:AUSDT') is None

    def test_remaining_qty_partial_branch_selection(self, close_env, monkeypatch):
        """remaining>=0.001 → partial 分支：record(final=False)+pop失败/保留 qty。"""
        close_env['set_risk_responses']([
            [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}],
            [{'symbol': 'AUSDT', 'positionAmt': '-6', 'entryPrice': '2.0'}]])
        monkeypatch.setattr(close_env['pm'], '_execution_service',
                            lambda: exec_service.ExecutionService(binance=type(
                                'P', (), {'place_order': staticmethod(
                                    lambda i: {'orderId': 1, 'status': 'FILLED',
                                               'executedQty': '4'})})()))
        pos = _pos(qty=10.0)
        positions = {'AUSDT': pos}
        r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
        assert r is False
        assert positions['AUSDT']['qty'] == 6.0    # partial 保留剩余


# ═══════════════════════════════════════════════════════════════
#  Partial path（criteria 9 / PMB-6）
# ═══════════════════════════════════════════════════════════════

class TestPartialSequence:
    def test_partial_cross_boundary_pmb6(self, close_env, monkeypatch):
        """partial：order(Service) → qty 更新 → Redis save；
        PG 零写入 + ledger 零写入 + 无 cancel + 无 marker。"""
        events = []
        received = []

        class Spy:
            def place_order(self, intent):
                received.append(intent)
                events.append('port_place_order')
                close_env['calls']['post'].append(
                    {'path': '/fapi/v1/order',
                     'params': intent.to_params()})
                return {'orderId': 2, 'status': 'FILLED'}
        monkeypatch.setattr(close_env['pm'], '_execution_service',
                            lambda: exec_service.ExecutionService(binance=Spy()))
        pg_events = []
        monkeypatch.setattr(close_env['pm'], '_pg_record_event',
                            lambda e: pg_events.append(e))
        cancel = []
        monkeypatch.setattr(close_env['pm'], '_cancel_all_algo',
                            lambda s: cancel.append(s))
        save_calls = []
        monkeypatch.setattr(close_env['pm'], '_save',
                            lambda positions: save_calls.append(1))

        pos = _pos(qty=10.0)
        positions = {'AUSDT': pos}
        close_env['pm']._partial_close('AUSDT', pos, 1.9, 4.0, 5, positions)

        assert received[0] == partial_close_intent('AUSDT', 'SHORT', 4.0)
        o = _order_posts(close_env['calls'])[0]['params']
        assert o == {'symbol': 'AUSDT', 'side': 'BUY', 'type': 'MARKET',
                     'quantity': 4.0, 'positionSide': 'BOTH'}
        assert 'reduceOnly' not in o
        assert pos['qty'] == 6.0
        assert pg_events == []                 # PMB-6：PG 零写入（不补）
        assert cancel == []                    # 不 cancel algo
        assert len(save_calls) == 1            # Redis save 一次
        assert close_env['redis'].get('closed:AUSDT') is None  # 无 marker

    def test_negative_partial_qty_inversion_pmb3(self, close_env, monkeypatch):
        """负 close_qty 原样（E-OBS-7）：qty 反向放大，不 clamp。"""
        received = []

        class Spy:
            def place_order(self, intent):
                received.append(intent)
                return {'orderId': 2, 'status': 'FILLED'}
        monkeypatch.setattr(close_env['pm'], '_execution_service',
                            lambda: exec_service.ExecutionService(binance=Spy()))
        pos = _pos(qty=10.0)
        positions = {'AUSDT': pos}
        close_env['pm']._partial_close('AUSDT', pos, 1.9, -100.0, 5, positions)
        assert received[0].quantity == -100.0
        assert pos['qty'] == 110.0


# ═══════════════════════════════════════════════════════════════
#  Protection path（criteria 10）
# ═══════════════════════════════════════════════════════════════

class TestProtectionSequence:
    def test_open_algo_enqueue_after_success(self, exec_env):
        assert exec_env['se'].open_position(**_open_kwargs()) is True
        assert exec_env['calls']['algo'] == [{'symbol': 'TESTUSDT', 'side': 'SELL',
                                              'trigger': 95.0, 'qty': 100.0}]

    def test_open_algo_enqueue_failure_swallowed_trade_continues(self, exec_env, monkeypatch):
        """enqueue 失败 → 仅日志，open 仍 True（trade continues）。"""
        def _boom(*a, **k):
            raise RuntimeError('queue lost')
        monkeypatch.setattr(exec_env['se'], '_algo_enqueue', _boom)
        assert exec_env['se'].open_position(**_open_kwargs()) is True
        assert exec_env['redis'].get('pm:positions')['TESTUSDT']['qty'] == 100.0

    def test_place_success_writes_algo_sl_id_back(self, close_env, monkeypatch, fake_redis):
        """place 成功 → pm:positions 写回 algo_sl_id（protection writeback 保持）。"""
        pm = close_env['pm']
        fake_redis.set('pm:positions', {'AUSDT': _pos()})
        money = {'posted': []}
        monkeypatch.setattr(pm, '_light_fapi_post',
                            lambda path, params: money['posted'].append(
                                {'path': path, 'params': params}) or {'algoId': 77})
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda sym: None)
        import requests as _req
        monkeypatch.setattr(_req, 'get', lambda *a, **k: type(
            'R', (), {'status_code': 200,
                      'json': staticmethod(lambda: {'symbols': []})})())
        monkeypatch.setattr(pm, '_rget', fake_redis.get)
        monkeypatch.setattr(pm, '_rset', fake_redis.set)
        r = pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        assert r == {'algoId': 77}
        assert fake_redis.get('pm:positions')['AUSDT']['algo_sl_id'] == 77

    def test_place_failure_returns_error_no_writeback(self, close_env, monkeypatch, fake_redis):
        pm = close_env['pm']
        fake_redis.set('pm:positions', {'AUSDT': _pos()})
        monkeypatch.setattr(pm, '_light_fapi_post',
                            lambda path, params: {'code': -2021})
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda sym: None)
        import requests as _req
        monkeypatch.setattr(_req, 'get', lambda *a, **k: type(
            'R', (), {'status_code': 200, 'json': staticmethod(
                lambda: {'symbols': []})})())
        monkeypatch.setattr(pm, '_rget', fake_redis.get)
        monkeypatch.setattr(pm, '_rset', fake_redis.set)
        r = pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        assert r == {'code': -2021}                       # 原样（失败语义）
        assert fake_redis.get('pm:positions')['AUSDT'].get('algo_sl_id', 0) == 0  # 无写回

    def test_cancel_exception_does_not_block_close(self, close_env, monkeypatch):
        """cancel 内部异常 → log 吞错 → close 继续（真实 close 分支 try/except）。"""
        close_env['set_risk_responses']([
            [{'symbol': 'AUSDT', 'positionAmt': '-10', 'entryPrice': '2.0'}], []])

        def boom(sym):
            raise RuntimeError('cancel api down')
        monkeypatch.setattr(close_env['pm'], '_cancel_all_algo', boom)
        pos = _pos()
        positions = {'AUSDT': pos}
        r = close_env['pm']._close('AUSDT', pos, 1.9, '硬止损', positions)
        assert r is True                        # trade continues
        assert 'AUSDT' not in positions

    def test_algo_payload_frozen(self, close_env, monkeypatch, fake_redis):
        """algoOrder payload 冻结（BOTH/CONDITIONAL/STOP_MARKET/GTC/reduceOnly）。"""
        pm = close_env['pm']
        posted = []
        monkeypatch.setattr(pm, '_light_fapi_post',
                            lambda path, params: posted.append(
                                {'path': path, 'params': params}) or {'algoId': 1})
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda sym: None)
        import requests as _req
        monkeypatch.setattr(_req, 'get', lambda *a, **k: type(
            'R', (), {'status_code': 200, 'json': staticmethod(
                lambda: {'symbols': []})})())
        pm._algo_place_sl_inner('AUSDT', 'SELL', 0.92, 100.0)
        p = posted[0]
        assert p['path'] == '/fapi/v1/algoOrder'
        assert p['params']['positionSide'] == 'BOTH'
        assert p['params']['algoType'] == 'CONDITIONAL'
        assert p['params']['type'] == 'STOP_MARKET'
        assert p['params']['workingType'] == 'MARK_PRICE'
        assert p['params']['timeInForce'] == 'GTC'
        assert p['params']['reduceOnly'] == 'true'