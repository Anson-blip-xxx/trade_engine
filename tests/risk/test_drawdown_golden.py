"""Golden：_drawdown_status / drawdown_mode 状态机全场景。

时间 freeze + Redis fake，deterministic。
"""

import pytest

from strategies import shared_executor as se


class FakeClock:
    ts = 1788000000.0

    @classmethod
    def time(cls):
        return cls.ts


@pytest.fixture
def dd_env(monkeypatch, fake_redis):
    """drawdown 测试环境：fake Redis + 可控余额 + 可控时间。"""
    state = {'balance': 4000.0}
    monkeypatch.setattr(se, '_rget', fake_redis.get)
    monkeypatch.setattr(se, '_rset', fake_redis.set)
    monkeypatch.setattr(se, '_get_balance', lambda: state['balance'])
    monkeypatch.setattr(se, 'time', FakeClock)
    return {'se': se, 'redis': fake_redis, 'state': state}


def _seed_peak(env, bal):
    env['redis'].set('account:peak', {'bal': bal, 'ts': FakeClock.time()})


def _seed_pause(env, *, ts, base_balance, loss_lock=False):
    env['redis'].set('account:dd_pause',
                     {'ts': ts, 'base_balance': base_balance, 'loss_lock': loss_lock})


# ── 1. Normal ────────────────────────────────────────────────────────────

class TestNormal:
    def test_no_peak(self, dd_env):
        r = dd_env['se']._drawdown_status()
        assert r == (1.0, 0.0)
        assert dd_env['se'].drawdown_mode() == 'normal'
        assert dd_env['redis'].get('account:peak') is not None

    def test_balance_equals_peak(self, dd_env):
        _seed_peak(dd_env, 4000)
        assert dd_env['se']._drawdown_status() == (1.0, 0.0)

    def test_balance_above_peak_updates(self, dd_env):
        _seed_peak(dd_env, 3000)
        dd_env['state']['balance'] = 5000.0
        r = dd_env['se']._drawdown_status()
        assert r == (1.0, 0.0)
        assert dd_env['redis'].get('account:peak')['bal'] == 5000.0

    def test_balance_zero(self, dd_env):
        dd_env['state']['balance'] = 0.0
        assert dd_env['se']._drawdown_status() == (1.0, 0.0)


# ── 2-3. Reduced（8% ≤ dd < 15%） ───────────────────────────────────────

class TestReduced:
    def test_dd_below_8_pct(self, dd_env):
        _seed_peak(dd_env, 1000)
        dd_env['state']['balance'] = 925.0  # dd = 7.5%
        assert dd_env['se']._drawdown_status() == (1.0, 7.5)

    def test_dd_exactly_8_pct(self, dd_env):
        _seed_peak(dd_env, 1000)
        dd_env['state']['balance'] = 920.0
        r = dd_env['se']._drawdown_status()
        assert r == (0.5, 8.0)
        assert dd_env['se'].drawdown_mode() == 'reduced'

    def test_dd_interior_reduced(self, dd_env):
        _seed_peak(dd_env, 4400)
        dd_env['state']['balance'] = 4000.0
        r = dd_env['se']._drawdown_status()
        assert r[0] == 0.5 and r[1] == pytest.approx((4400-4000)/4400*100)
        assert dd_env['se'].drawdown_mode() == 'reduced'


# ── 4-5. Pause（dd >= 15%） ─────────────────────────────────────────────

class TestPause:
    def test_dd_15_first_detection(self, dd_env):
        _seed_peak(dd_env, 1000)
        dd_env['state']['balance'] = 850.0
        r = dd_env['se']._drawdown_status()
        assert r == (0.0, 15.0)
        assert dd_env['se'].drawdown_mode() == 'halt'
        pause = dd_env['redis'].get('account:dd_pause')
        assert pause['loss_lock'] is False
        assert pause['base_balance'] == 850.0

    def test_dd_20_first_detection(self, dd_env):
        _seed_peak(dd_env, 5000)
        dd_env['state']['balance'] = 4000.0
        r = dd_env['se']._drawdown_status()
        assert r == (0.0, 20.0)
        assert dd_env['se'].drawdown_mode() == 'halt'


# ── 6. Pause delay < 4h ─────────────────────────────────────────────────

class TestPauseDelay:
    def test_within_4h_still_halt(self, dd_env):
        _seed_peak(dd_env, 5000)
        _seed_pause(dd_env, ts=FakeClock.time() - 3600, base_balance=4000)
        dd_env['state']['balance'] = 4000.0
        r = dd_env['se']._drawdown_status()
        assert r == (0.0, 20.0)
        assert dd_env['se'].drawdown_mode() == 'halt'


# ── 7-8. Recovery（paused >= 4h） ───────────────────────────────────────

class TestRecovery:
    def test_after_4h_recovery_factor(self, dd_env):
        _seed_peak(dd_env, 5000)
        _seed_pause(dd_env, ts=FakeClock.time() - 5 * 3600, base_balance=4000)
        dd_env['state']['balance'] = 4000.0
        r = dd_env['se']._drawdown_status()
        assert r == (0.25, 20.0)
        assert dd_env['se'].drawdown_mode() == 'recovery'

    def test_recovery_factor_is_025(self, dd_env):
        assert dd_env['se']._DD_RECOVERY_FACTOR == 0.25


# ── 9. Recovery loss lock ───────────────────────────────────────────────

class TestRecoveryLossLock:
    def test_balance_drops_2pct_from_base(self, dd_env):
        _seed_peak(dd_env, 5000)
        _seed_pause(dd_env, ts=FakeClock.time() - 5 * 3600,
                    base_balance=4000, loss_lock=False)
        dd_env['state']['balance'] = 3900.0
        r = dd_env['se']._drawdown_status()
        assert r == (0.0, pytest.approx(22.0))
        assert dd_env['se'].drawdown_mode() == 'halt'
        pause = dd_env['redis'].get('account:dd_pause')
        assert pause['loss_lock'] is True
        assert pause['base_balance'] == 3900.0

    def test_balance_not_dropped_enough_no_lock(self, dd_env):
        _seed_peak(dd_env, 5000)
        _seed_pause(dd_env, ts=FakeClock.time() - 5 * 3600,
                    base_balance=4000, loss_lock=False)
        dd_env['state']['balance'] = 3950.0
        r = dd_env['se']._drawdown_status()
        assert r == (0.25, 21.0)
        assert dd_env['redis'].get('account:dd_pause')['loss_lock'] is False


# ── 10. Retry delay（loss_lock 后 6h） ──────────────────────────────────

class TestRetryDelay:
    def test_loss_lock_within_6h_halt(self, dd_env):
        _seed_peak(dd_env, 5000)
        _seed_pause(dd_env, ts=FakeClock.time() - 3 * 3600,
                    base_balance=3900, loss_lock=True)
        dd_env['state']['balance'] = 3900.0
        r = dd_env['se']._drawdown_status()
        assert r == (0.0, 22.0)
        assert dd_env['se'].drawdown_mode() == 'halt'

    def test_loss_lock_after_6h_resets_to_halt(self, dd_env):
        _seed_peak(dd_env, 5000)
        _seed_pause(dd_env, ts=FakeClock.time() - 7 * 3600,
                    base_balance=3900, loss_lock=True)
        dd_env['state']['balance'] = 3900.0
        r = dd_env['se']._drawdown_status()
        assert r == (0.0, 22.0)
        pause = dd_env['redis'].get('account:dd_pause')
        assert pause['loss_lock'] is False
        assert pause['ts'] == FakeClock.time()


# ── 11. Recovery exit（dd 降到 <15%） ───────────────────────────────────

class TestRecoveryExit:
    def test_dd_drops_below_15_clears_pause(self, dd_env):
        _seed_peak(dd_env, 5000)
        _seed_pause(dd_env, ts=FakeClock.time() - 3600, base_balance=4000)
        dd_env['state']['balance'] = 4300.0  # dd = 14% < 15%
        r = dd_env['se']._drawdown_status()
        assert r[0] == 0.5 and r[1] == pytest.approx((5000-4300)/5000*100)
        assert dd_env['se'].drawdown_mode() == 'reduced'
        assert dd_env['redis'].get('account:dd_pause') == {}


# ── 12. New peak ────────────────────────────────────────────────────────

class TestNewPeak:
    def test_balance_above_peak(self, dd_env):
        _seed_peak(dd_env, 3000)
        dd_env['state']['balance'] = 5000.0
        dd_env['se']._drawdown_status()
        assert dd_env['redis'].get('account:peak')['bal'] == 5000.0
        assert dd_env['se'].drawdown_mode() == 'normal'


# ── Edge cases ──────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_peak_bal_zero(self, dd_env):
        """peak.bal = 0 → factor=1.0（peak_bal <= 0 guard）。"""
        _seed_peak(dd_env, 0)
        dd_env['state']['balance'] = 4000.0
        assert dd_env['se']._drawdown_status() == (1.0, 0.0)

    def test_dd_pause_missing_base_balance(self, dd_env):
        """dd_pause 缺 base_balance → 用当前 balance 填充。"""
        _seed_peak(dd_env, 5000)
        dd_env['redis'].set('account:dd_pause', {'ts': FakeClock.time() - 3600})
        dd_env['state']['balance'] = 4000.0
        r = dd_env['se']._drawdown_status()
        assert r == (0.0, 20.0)
        pause = dd_env['redis'].get('account:dd_pause')
        assert pause['base_balance'] == 4000.0

    def test_malformed_peak_string_raises(self, dd_env):
        """Observed Current Behavior：peak 为 str → AttributeError（无 try/except）。
        Potential concern: 缺少 defensive 处理。
        Future phase: Phase 7。"""
        dd_env['redis'].set('account:peak', 'garbage')
        dd_env['state']['balance'] = 4000.0
        with pytest.raises(AttributeError):
            dd_env['se']._drawdown_status()
