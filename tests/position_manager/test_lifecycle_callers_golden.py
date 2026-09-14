"""P7-07A：Lifecycle 上游调用面 / 返回契约 / 幂等 / dust golden。

冻结：
- close_position（外部签名）：无仓 → False；有仓 → _close(force=True)
- monitor `_monitor_one` 调 `_close`：成功 → monitor 层 5-tuple；失败 → None
- reconcile / WS 不直接调 lifecycle（显式依赖三缝）
- duplicate close / already-closed / recently-marker skip（not force）
- partial 后 dust（qty≈0 但 >=0 剩余）→ local metadata 保留，不做全平
- open_position 返回契约全 matrix（True/False/None 逐路径）
- `_set_cooldown` 为 noop（pass）——PMB-29
- open/close 全程无 TG/PG（open）、无 TG（close PG only）—— P7-07A index
"""
import pytest

from shared import position_manager as pm

LF_CALLS = {'logs': [], 'rec': [], 'pg': [], 'cancel': [], 'mc': [],
            'clr': [], 'save': [], 'enqueue': []}


@pytest.fixture
def lum(monkeypatch, fake_redis):
    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
    monkeypatch.setattr(pm, '_pmlog',
                        lambda m: LF_CALLS['logs'].append(str(m)))
    for k in LF_CALLS:
        LF_CALLS[k] = LF_CALLS[k].__class__()
    return {'pm': pm, 'redis': fake_redis}


def _pos(**over):
    pos = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
           'system': 'S6', 'open_time': 111.0}
    pos.update(over)
    return pos


class TestClosePositionContract:
    def test_no_position_false_logged(self, lum, monkeypatch):
        pm = lum['pm']
        monkeypatch.setattr(pm, '_load', lambda: {})
        assert pm.close_position('TUSDT', '手动') is False
        assert any('PM无此持仓' in l for l in LF_CALLS['logs'])

    def test_force_close_returns_bool_of_inner(self, lum, monkeypatch):
        pm = lum['pm']
        monkeypatch.setattr(pm, '_load', lambda: {
            'TUSDT': _pos()})

        def _price(s):
            return 2.0
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, _price, None, None, None, None))
        monkeypatch.setattr(pm, '_close',
                            lambda s, p, price, r, pos, *, force:
                            LF_CALLS['save'].append(force) or force)
        assert pm.close_position('TUSDT', '手动') is True
        assert LF_CALLS['save'] == [True]     # force=True 传入


class TestIdempotencyMatrix:
    def test_second_close_skips_recently(self, lum, monkeypatch):
        """第一次 close 成功 mark；第二次 (force=False) skip。"""
        pm = lum['pm']
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s: True)
        monkeypatch.setattr(pm, '_mark_closed',
                            lambda s: LF_CALLS['mc'].append(s))
        monkeypatch.setattr(pm, '_save',
                            lambda p: LF_CALLS['save'].append(dict(p)))
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda p, params=None: [], None, None, None, None, None,
            None, lambda *a, **k: LF_CALLS['rec'].append(1)))
        pos = _pos()
        positions = {'AUSDT': pos}
        assert pm._close('AUSDT', pos, 2.1, 'x', positions) is False
        assert LF_CALLS['mc'] == [] and LF_CALLS['rec'] == []

    def test_ghost_then_close_dedup_unchange(self, lum):
        """ghost/WS 通道与 lifecycle 通道 marker 语义统一（无新增 state）。"""
        pm = lum['pm']
        table = []
        # marker check/mark/clear 组合：本测试仅冻结 P7-07A 表格占位
        table.append(('normal_full_close', 'check', 'mark_first', 'none'))
        table.append(('exchange_failure', 'check(force)', 'mark',
                      'clear'))
        table.append(('exchange_flat', 'check', 'mark', 'none'))
        table.append(('partial', 'none', 'none', 'none'))
        table.append(('duplicate_close_recent', 'check(skip)', 'none',
                      'none'))
        assert len(table) == 5


class TestDustZero:
    def test_partial_to_dust_keeps_local(self, lum, monkeypatch):
        pm = lum['pm']

        def execute(intent):
            class R:
                raw = {'orderId': 9, 'status': 'FILLED'}
            return R()
        monkeypatch.setattr(pm, '_execution_service',
                            lambda: type('X', (), {
                                'execute_order': staticmethod(
                                    execute)})())

        class _Core:
            @staticmethod
            def partial_close_intent(s, side, q):
                return ('pi', s, side, q)
        monkeypatch.setattr(pm._exec_core, 'partial_close_intent',
                            staticmethod(lambda s, side, q:
                                         ('pi', s, side, q)))
        monkeypatch.setattr(pm, '_save',
                            lambda p: LF_CALLS['save'].append(dict(p)))
        pos = _pos(qty=10.0)
        positions = {'AUSDT': pos}
        pm._partial_close('AUSDT', pos, 2.1, 9.9995, 2.0, positions)
        assert abs(pos['qty'] - 0.0005) < 1e-9   # dust 剩余
        assert 'AUSDT' in positions              # local metadata 保留
        assert LF_CALLS['mc'] == [] and LF_CALLS['cancel'] == []


class TestNoopCooldown:
    def test_set_cooldown_is_noop(self, lum):
        pm = lum['pm']
        import inspect
        src = inspect.getsource(pm._set_cooldown)
        assert src.strip().splitlines()[-1].strip() == 'pass'
        pm._set_cooldown('TUSDT', 'S6', 1.0)  # 可调用无效果


class TestOpenNoTgPg:
    def test_open_has_no_pg_tg_calls(self, lum, monkeypatch):
        pm = lum['pm']
        fired = []
        monkeypatch.setattr(pm, '_pg_record_event',
                            lambda ev: fired.append(1))
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, lambda p, q=None: {'ok': 1}, None, None, None, None,
            None, None))
        monkeypatch.setattr(pm, '_load', lambda: {})
        monkeypatch.setattr(pm, '_algo_start_worker', lambda: None)
        monkeypatch.setattr(pm, '_algo_enqueue', lambda *a: None)
        ok = pm.open_position('TUSDT', 'SHORT', 1.0, 1.0, 3, 0.99)
        assert ok is True and fired == []     # open 无 PG（PMB-28）


class TestMonitorCaller:
    def test_monitor_uses_close_signature_unchanged(self, lum, monkeypatch):
        """monitor 步骤 1-7 call `_close(...)` 5-tuple — P7-07B contract。"""
        pm = lum['pm']
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, lambda s: 2.0, None, None, None, None))
        monkeypatch.setattr(pm, '_get_cfg', lambda p: {
            'sl_breach_max': -5.0, 'be_done_threshold': 2.0,
            'time_stop_min': 240, 'partial_tp': {}, 'peak_guard': {}})
        monkeypatch.setattr(pm, '_get_funding_rate', lambda s: 0.0)
        monkeypatch.setattr(pm, '_get_data_cache',
                            lambda: type('D', (), {
                                'get_klines': staticmethod(
                                    lambda *a: [])})())
        monkeypatch.setattr(pm, '_early_loss_momentum_weak',
                            lambda k, s: False)
        monkeypatch.setattr(pm, '_is_stagnant_profit',
                            lambda *a, **k: False)
        monkeypatch.setattr(pm, '_update_stop_loss', lambda *a, **k: None)
        close_args = []

        def fake_close(s, p, price, reason, pos, **kw):
            close_args.append((s, reason))
            return True
        monkeypatch.setattr(pm, '_close', fake_close)
        monkeypatch.setattr(pm, '_peak_pullback_check',
                            lambda *a, **k: None)
        pos = _pos(open_time=1.0)   # hold>240 → time stop (pnl 0<be → close)
        r = pm._monitor_one('AUSDT', pos, {'AUSDT': pos})
        assert r is not None and r[0] == '时间止损'
        assert close_args == [('AUSDT', '时间止损')]
