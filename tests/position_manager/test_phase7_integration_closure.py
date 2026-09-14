"""P7-08：Phase 7 跨服务集成 smoke / parity（复用既有 golden 骨架，不重复全量）。"""
import time as time_mod

import pytest

from shared import position_manager as pm


@pytest.fixture
def it(monkeypatch, fake_redis):
    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    monkeypatch.setattr(pm, '_monitor_heartbeat_ts', 0.0)
    monkeypatch.setattr(pm, '_RECENTLY_GHOSTED', [])
    monkeypatch.setattr(pm, '_CLOSE_ERROR_LOG_TS', {})
    monkeypatch.setattr(pm, '_light_fapi_get', lambda p, params=None: [])
    monkeypatch.setattr(pm, '_light_fapi_delete',
                        lambda p, params=None: {})
    monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
    monkeypatch.setattr(pm, '_was_closed_recently', lambda s: False)
    monkeypatch.setattr(pm, '_pmlog', lambda *a, **k: None)
    return {'pm': pm, 'redis': fake_redis}


def _rec_s6(monkeypatch, rec_calls=None, price=None, risk=None, post=None):
    calls = rec_calls if rec_calls is not None else []

    def rec(*a, **kw):
        calls.append((a, kw))
    monkeypatch.setattr(pm, '_s6api', lambda: (
        (lambda p, params=None: risk or []) if risk is not None
        else None, lambda p, q=None: {'orderId': 9}, None,
        price or (lambda s: 2.0), lambda s: (6, 6),
        lambda *a, **k: (0, 0, 0), lambda *a, **k: 50.0, rec))
    return calls


def _pos(**over):
    pos = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
           'system': 'S6', 'open_time': 111.0, 'sl': 1.5}
    pos.update(over)
    return pos


class TestLifecycleThroughFacade:
    def test_open_success_via_service(self, it, monkeypatch):
        pm = it['pm']
        monkeypatch.setattr(pm, '_algo_start_worker', lambda: None)
        monkeypatch.setattr(pm, '_algo_enqueue', lambda *a: None)
        ok = pm.open_position('NEWUSDT', 'SHORT', 2.0, 10.0, 3, 1.9,
                              system='S6')
        assert ok is True
        assert it['redis'].get('pm:positions').get('NEWUSDT', {})

    def test_duplicate_open_returns_true_state_unchanged(self, it,
                                                         monkeypatch):
        pm = it['pm']
        pm._save({'TUSDT': _pos()})
        monkeypatch.setattr(pm, '_algo_start_worker', lambda: None)
        enq = []
        monkeypatch.setattr(pm, '_algo_enqueue',
                            lambda s, side, sl, q: enq.append(1))
        ok = pm.open_position('TUSDT', 'SHORT', 2.0, 10.0, 3, 1.9)
        assert ok is True and enq == [1]         # PMB-30 冒烟：SL 已孤立入队
        assert it['redis'].get('pm:positions')['TUSDT']['entry'] == 2.0

    def test_full_close_via_facade(self, it, monkeypatch):
        pm = it['pm']
        monkeypatch.setattr(pm._exec_core, 'close_intent',
                            staticmethod(lambda s, side, q:
                                         ('ci', s, side, q)))

        def exec_order(intent):
            class R:
                raw = {'orderId': 9, 'status': 'FILLED',
                       'executedQty': '10'}
            return R()
        monkeypatch.setattr(pm, '_execution_service',
                            lambda: type('X', (), {
                                'execute_order': staticmethod(
                                    exec_order)})())
        monkeypatch.setattr(pm, '_cancel_all_algo', lambda s: None)
        monkeypatch.setattr(pm, '_pg_record_event', lambda ev: None)
        root = pm._load
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda p, params=None: [], lambda p, q=None: {'orderId': 9},
            None, None, None, None, None, lambda *a, **k: None))
        monkeypatch.setattr(pm, '_round_qty', lambda s, q: round(q, 6))
        pos = _pos()
        positions = {'AUSDT': pos}
        state = {}
        monkeypatch.setattr(pm, '_save', lambda p: state.update(p))
        ok = pm._close('AUSDT', pos, 2.1, '硬止损', positions)
        assert ok is True and 'AUSDT' not in state

    def test_exchange_flat_true_no_exec(self, it, monkeypatch):
        pm = it['pm']

        def exec_order(intent):
            raise AssertionError('exchange-flat 不应执行市价平仓')
        executed = []
        monkeypatch.setattr(pm, '_cancel_all_algo',
                            lambda s: executed.append(s))
        monkeypatch.setattr(pm, '_pg_record_event', lambda ev: None)
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda p, params=None: [], None, None, None, None, None,
            None, lambda *a, **k: None))
        monkeypatch.setattr(pm, '_execution_service',
                            lambda: type('X', (), {
                                'execute_order': staticmethod(
                                    exec_order)})())
        pos = _pos()
        positions = {'AUSDT': pos}
        state = {}
        monkeypatch.setattr(pm, '_save', lambda p: state.update(p))
        ok = pm._close('AUSDT', pos, 2.1, '硬止损', positions)
        assert ok is True and executed == ['AUSDT']

    def test_partial_close_leaves_ledger_untouched(self, it, monkeypatch):
        pm = it['pm']
        monkeypatch.setattr(pm._exec_core, 'partial_close_intent',
                            staticmethod(lambda s, side, q:
                                         ('pi', s, side, q)))

        def exec_order(intent):
            class R:
                raw = {'orderId': 9, 'status': 'FILLED'}
            return R()
        monkeypatch.setattr(pm, '_execution_service',
                            lambda: type('X', (), {
                                'execute_order': staticmethod(
                                    exec_order)})())
        monkeypatch.setattr(pm, '_save', lambda p: None)
        monkeypatch.setattr(pm, '_mark_closed',
                            lambda s: (_ for _ in ()).throw(
                                AssertionError('partial 不 mark')))
        monkeypatch.setattr(pm, '_cancel_all_algo',
                            lambda s: (_ for _ in ()).throw(
                                AssertionError('partial 不 cancel')))
        pos = _pos()
        positions = {'AUSDT': pos}
        pm._partial_close('AUSDT', pos, 2.1, 4.0, 2.0, positions)
        assert pos['qty'] == 6.0          # 0 ledger + no cancel（冒烟）


class TestMonitorReconcileWSIntegration:
    def test_monitor_empty_no_close(self, it):
        it['pm']._save({})
        assert it['pm'].monitor_all() == []

    def test_monitor_decision_triggers_time_stop_close(self, it,
                                                      monkeypatch):
        """monitor → decision → Lifecycle close 冒烟。"""
        pm = it['pm']
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, lambda s: 1.99, None, None, None, None))
        monkeypatch.setattr(pm, '_get_cfg', lambda p: {
            'sl_breach_max': -5.0, 'be_done_threshold': 2.0,
            'time_stop_min': 240, 'partial_tp': {}, 'peak_guard': {}})
        monkeypatch.setattr(pm, '_get_funding_rate', lambda s: 0.0)
        monkeypatch.setattr(pm, '_get_data_cache', lambda: type('D', (), {
            'get_klines': staticmethod(lambda *a: [])})())
        monkeypatch.setattr(pm, '_early_loss_momentum_weak',
                            lambda k, s: False)
        monkeypatch.setattr(pm, '_is_stagnant_profit',
                            lambda *a, **k: False)
        monkeypatch.setattr(pm, '_update_stop_loss', lambda *a, **k: None)
        close_args = []
        monkeypatch.setattr(
            pm, '_close', lambda s, p, price, reason, pos, **kw:
            close_args.append(reason) or True)
        monkeypatch.setattr(pm, '_peak_pullback_check',
                            lambda *a, **k: None)
        pos = _pos(sl=2.5, open_time=1.0)   # hold>240 → time stop
        r = pm._monitor_one('AUSDT', pos, {'AUSDT': pos})
        assert r is not None and r[0] == '时间止损'

    def test_reconcile_ghost_smoke(self, it, monkeypatch):
        pm = it['pm']
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda p, params=None: [], None, None, None, None,
            None, None, None))
        pm._save({'GHOSTUSDT': {'entry': 1.0}})
        ghost, missing = pm.reconcile_all()
        assert ghost == ['GHOSTUSDT'] and missing == []

    def test_ws_close_dedup_smoke(self, it):
        """WS record→mark → meta_filtered 丢弃 → reconcile 不再触发。"""
        pm = it['pm']
        import time as t
        pm._rset('closed:TUSDT', {'ts': t.time()})
        assert 'TUSDT' not in pm._load()
        assert pm.monitor_all() == []
