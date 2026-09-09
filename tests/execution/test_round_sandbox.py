"""Golden：_round_qty + Sandbox 拦截行为。"""
import pytest

from strategies import shared_executor as se


# ── _round_qty ──────────────────────────────────────────────────────────

class TestRoundQty:
    def test_lot_size_step(self, monkeypatch):
        """exchangeInfo LOT_SIZE stepSize → 按精度舍入。"""
        info = {'symbols': [{'symbol': 'XUSDT', 'filters': [
            {'filterType': 'LOT_SIZE', 'stepSize': '0.01000000'}]}]}

        def fapi_get(path, params=None):
            return info
        monkeypatch.setattr(se, 'fapi_get', fapi_get)
        assert se._round_qty('XUSDT', 1.23456) == 1.23

    def test_lot_size_step_1(self, monkeypatch):
        """stepSize=1 → 向下取整到整数（qty % step 截断，非四舍五入）。"""
        info = {'symbols': [{'symbol': 'XUSDT', 'filters': [
            {'filterType': 'LOT_SIZE', 'stepSize': '1.00000000'}]}]}

        def fapi_get(path, params=None):
            return info
        monkeypatch.setattr(se, 'fapi_get', fapi_get)
        assert se._round_qty('XUSDT', 17508.9) == 17508.0

    def test_exchange_info_exception_fallback(self, monkeypatch):
        """exchangeInfo 异常 → 返回原始 qty（当前行为，不优化）。"""
        def fapi_get(path, params=None):
            raise RuntimeError('network error')
        monkeypatch.setattr(se, 'fapi_get', fapi_get)
        assert se._round_qty('XUSDT', 1.23456789) == 1.23456789

    def test_exchange_info_missing_symbol(self, monkeypatch):
        """symbol 不在 exchangeInfo 中 → 返回原始 qty。"""
        info = {'symbols': []}

        def fapi_get(path, params=None):
            return info
        monkeypatch.setattr(se, 'fapi_get', fapi_get)
        assert se._round_qty('XUSDT', 1.23456789) == 1.23456789


# ── Sandbox ─────────────────────────────────────────────────────────────

class TestSandboxExecutor:
    def test_sandbox_check_off(self, monkeypatch):
        """沙盘关闭 → fapi_post 正常调用（返回 None→不开仓）。"""
        monkeypatch.setattr(se, '_sandbox_check', lambda: False)
        assert se._sandbox_check() is False

    def test_sandbox_check_on(self, monkeypatch):
        """沙盘开启 → _sandbox_check True。"""
        monkeypatch.setattr(se, '_sandbox_check', lambda: True)
        assert se._sandbox_check() is True

    def test_sandbox_post_intercept(self, monkeypatch):
        """沙盘开启 → fapi_post 被拦截返回 mock 结果。"""
        mock_result = {'orderId': 999, 'status': 'FILLED'}
        monkeypatch.setattr(se, '_sandbox_check', lambda: True)
        monkeypatch.setattr(se, '_sandbox_post',
                            lambda path, params: mock_result)
        r = se.fapi_post('/fapi/v1/order', {'symbol': 'X'})
        assert r == mock_result


class TestSandboxPM:
    def test_pm_sandbox_active_false(self, monkeypatch):
        from shared import position_manager as pm
        monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
        assert pm._sandbox_active() is False

    def test_pm_sandbox_active_true(self, monkeypatch):
        from shared import position_manager as pm
        monkeypatch.setattr(pm, '_sandbox_active', lambda: True)
        assert pm._sandbox_active() is True

    def test_pm_close_sandbox_mode(self, monkeypatch, fake_redis):
        """PM._close 沙盘模式 → 不调 Binance，直接记录。"""
        from shared import position_manager as pm
        monkeypatch.setattr(pm, '_sandbox_active', lambda: True)
        monkeypatch.setattr(pm, '_rget', fake_redis.get)
        monkeypatch.setattr(pm, '_rset', fake_redis.set)
        monkeypatch.setattr(pm, '_pmlog', lambda *a, **k: None)
        monkeypatch.setattr(pm, '_pg_record_event', lambda *a, **k: None)

        record_calls = []
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, None, None, None, None,
            lambda *a, **kw: record_calls.append({'args': a, 'kwargs': kw})))

        pos = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
               'system': 'S8', 'open_time': 1788000000, 'leverage': 3,
               'sl': 2.2, 'signal_type': 'TREND_DOWN', 'event_type': 'TREND_DOWN',
               'score': 66, 'be_done': False}
        positions = {'AUSDT': pos}
        r = pm._close('AUSDT', pos, 1.9, '硬止损', positions)
        assert r is True
        assert 'AUSDT' not in positions
        assert len(record_calls) == 1
        # 沙盘路径无 order POST
        # pnl = (2.0 - 1.9) * 10 = 1.00（SHORT 正确方向）
