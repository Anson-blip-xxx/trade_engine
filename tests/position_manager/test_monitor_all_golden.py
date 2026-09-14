"""P7-05A：monitor_all exact ordering golden（heartbeat/ghost/step/save/spy）。"""
import pytest

from shared import position_manager as pm


@pytest.fixture
def mon(monkeypatch, fake_redis):
    """monitor_all 隔离：Redis + log + save 监控 + ghost/monitor_one spy。"""
    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    monkeypatch.setattr(pm, '_monitor_heartbeat_ts', 0.0)
    monkeypatch.setattr(pm, '_RECENTLY_GHOSTED', [])
    # REST 轮询层短路（_load 直连 _light_fapi_get，绕过 _rget seam）
    monkeypatch.setattr(pm, '_light_fapi_get', lambda path, params=None: [])
    monkeypatch.setattr(pm, '_light_fapi_delete', lambda path, params=None: {})
    ghost_calls = []
    one_calls = []
    save_calls = []

    def _save(p):
        save_calls.append(dict(p))
        fake_redis.set('pm:positions', p)   # spy + 真写（_load REST/mer 走真链）
    monkeypatch.setattr(pm, '_save', _save)
    monkeypatch.setattr(pm, '_ghost_cleanup',
                        lambda p, f: ghost_calls.append(dict(p)) or [])

    def _monitor_one(symbol, pos, all_pos):
        one_calls.append(symbol)
        return None
    monkeypatch.setattr(pm, '_monitor_one', _monitor_one)
    return {'pm': pm, 'redis': fake_redis, 'ghost_calls': ghost_calls,
            'one_calls': one_calls, 'save_calls': save_calls}


class TestEmptyPositions:
    def test_empty_returns_empty_list(self, mon):
        if mon['pm']._load():
            mon['pm']._save({})
        assert mon['pm'].monitor_all() == []

    def test_empty_heartbeat_logged_then_throttled_60s(self, mon, monkeypatch):
        pm = mon['pm']
        pm._save({})
        logs = []
        monkeypatch.setattr(pm, '_pmlog', lambda m: logs.append(m))
        pm.monitor_all()
        assert any('监控心跳' in l for l in logs)
        logs.clear()
        pm.monitor_all()                       # 60s 内 → 重打 log 被节流
        assert logs == []

    def test_empty_heartbeat_refired_after_60s(self, mon, monkeypatch):
        pm = mon['pm']
        pm._save({})
        pm.monitor_all()
        pm._monitor_heartbeat_ts -= 61
        logs = []
        monkeypatch.setattr(pm, '_pmlog', lambda m: logs.append(m))
        pm.monitor_all()
        assert any('监控心跳' in l for l in logs)


class TestHeartbeatSnapshot:
    """非空持仓时：心跳行带 <input symbol> + side 首字母 + PnL。"""

    def _seed_two(self, mon):
        mon['pm']._save({'S8USDT': {'entry': 100.0, 'side': 'SHORT'},
                         'S6USDT': {'entry': 100.0, 'side': 'LONG'}})

    def test_heartbeat_contains_price_pnl(self, mon, monkeypatch):
        pm = mon['pm']
        self._seed_two(mon)
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, lambda s: 110.0, None, None, None, None))
        logs = []
        monkeypatch.setattr(pm, '_pmlog', lambda m: logs.append(m))
        pm._monitor_heartbeat_ts -= 61
        pm.monitor_all()
        line = next(l for l in logs if '监控心跳' in l and 'S8USDT' in l)
        assert 'S8USDT(S' in line and 'S6USDT(L' in line

    def test_heartbeat_get_price_exception_falls_back(self, mon, monkeypatch):
        pm = mon['pm']
        self._seed_two(mon)

        def boom(sym):
            raise RuntimeError('api')
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, boom, None, None, None, None))
        logs = []
        monkeypatch.setattr(pm, '_pmlog', lambda m: logs.append(m))
        pm._monitor_heartbeat_ts -= 61
        pm.monitor_all()                       # 不 raise
        assert any('监控心跳' in l and 'S8USDT' in l for l in logs)

    def test_heartbeat_capped_at_8_positions(self, mon, monkeypatch):
        pm = mon['pm']
        pm._save({f'S{i}USDT': {'entry': 1.0, 'side': 'LONG'}
                  for i in range(12)})
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, lambda s: 1.0, None, None, None, None))
        logs = []
        monkeypatch.setattr(pm, '_pmlog', lambda m: logs.append(m))
        pm._monitor_heartbeat_ts -= 61
        pm.monitor_all()
        line = next(l for l in logs if '监控心跳' in l)
        assert 'S7USDT' in line and 'S11USDT' not in line


class TestSweepOrdering:
    def test_step0_ghost_before_filter_before_monitor_one(self, mon):
        pm = mon['pm']
        pm._save({'AUSDT': {'entry': 5.0, 'side': 'SHORT', 'system': 'S8'},
                  'BUSDT': {'entry': 6.0, 'side': 'LONG', 'system': 'S6'}})
        out = pm.monitor_all('S6')
        # Step 0 ghost 在 filter 之前全量执行；_monitor_one 只走过滤后的仓
        assert len(mon['ghost_calls']) == 1
        assert mon['one_calls'] == ['BUSDT']
        assert out == []

    def test_no_filter_iterates_all_in_insertion_order(self, mon):
        pm = mon['pm']
        pm._save({'AUSDT': {'entry': 5.0, 'side': 'SHORT'},
                  'BUSDT': {'entry': 6.0, 'side': 'LONG'}})
        pm.monitor_all()
        assert mon['one_calls'] == ['AUSDT', 'BUSDT']

    def test_monitor_one_result_appended(self, mon, monkeypatch):
        pm = mon['pm']
        pm._save({'AUSDT': {'entry': 5.0, 'side': 'SHORT'}})
        monkeypatch.setattr(pm, '_monitor_one',
                            lambda s, p, ap: ('追踪锁利', 5.5))
        out = pm.monitor_all()
        assert out == [('AUSDT', '追踪锁利', 5.5)]

    def test_monitor_one_exception_isolated_per_symbol(self, mon, monkeypatch):
        """P7-00 top risk：单仓 _monitor_one raise → 后续仓继续。"""
        pm = mon['pm']
        pm._save({'BADUSDT': {'entry': 1.0, 'side': 'SHORT'},
                  'OKUSDT': {'entry': 2.0, 'side': 'LONG'}})

        def _one(symbol, pos, all_pos):
            if symbol == 'BADUSDT':
                raise RuntimeError('boom')
            mon['one_calls'].append(symbol)
            return None
        monkeypatch.setattr(pm, '_monitor_one', _one)
        logs = []
        monkeypatch.setattr(pm, '_pmlog', lambda m: logs.append(m))
        pm.monitor_all()
        assert any('监控异常' in l and 'BADUSDT' in l for l in logs)
        assert mon['one_calls'] == ['OKUSDT']

    def test_ghost_closed_prepended_to_result(self, mon, monkeypatch):
        pm = mon['pm']
        pm._save({'AUSDT': {'entry': 5.0, 'side': 'SHORT', 'system': 'S8'}})
        monkeypatch.setattr(pm, '_ghost_cleanup',
                            lambda p, f: [('AUSDT', '手动平仓', 5.2)])
        out = pm.monitor_all('S8')
        assert out == [('AUSDT', '手动平仓', 5.2)]


class TestGhostQueueConsumption:
    def _seed(self, mon):
        mon['pm']._save({'AUSDT': {'entry': 5.0, 'side': 'SHORT',
                                   'system': 'S8'}})

    def test_ghost_queue_drained_on_match(self, mon, monkeypatch):
        pm = mon['pm']
        self._seed(mon)
        pm._RECENTLY_GHOSTED.append(('BUSDT', '硬止损', 5.0, 5.0, 10.0, 'SHORT'))
        out = pm.monitor_all('S8')
        assert any(c[0] == 'BUSDT' for c in out)
        assert pm._RECENTLY_GHOSTED == []

    def test_ghost_queue_opposite_side_requeued(self, mon):
        pm = mon['pm']
        self._seed(mon)
        pm._RECENTLY_GHOSTED.append(('BUSDT', '硬止损', 5.0, 5.0, 10.0, 'LONG'))
        pm.monitor_all('S8')                  # S8 只收 SHORT ghost
        assert pm._RECENTLY_GHOSTED == [
            ('BUSDT', '硬止损', 5.0, 5.0, 10.0, 'LONG')]

    def test_ghost_queue_no_filter_always_consumed(self, mon):
        pm = mon['pm']
        self._seed(mon)
        pm._RECENTLY_GHOSTED.append(('BUSDT', '硬止损', 5.0, 5.0, 10.0, 'LONG'))
        pm.monitor_all()
        assert pm._RECENTLY_GHOSTED == []

    def test_ghost_queue_missing_side_consumed_even_under_filter(self, mon):
        """len(g)<6 → g_side=None → `not g_side` 直接消费（filter 失效）。"""
        pm = mon['pm']
        self._seed(mon)
        pm._RECENTLY_GHOSTED.append(('BUSDT', 'x', 5.0, 5.0, 10.0))
        out = pm.monitor_all('S8')
        assert any(c[0] == 'BUSDT' for c in out)
        assert pm._RECENTLY_GHOSTED == []

    def test_ghost_queue_all_s6_allows_long_only(self, mon):
        pm = mon['pm']
        self._seed(mon)
        pm._RECENTLY_GHOSTED.append(('B1', 'x', 1, 2, 3, 'LONG'))
        pm._RECENTLY_GHOSTED.append(('B2', 'x', 1, 2, 3, 'SHORT'))
        pm.monitor_all('S6')
        assert pm._RECENTLY_GHOSTED == [('B2', 'x', 1, 2, 3, 'SHORT')]

    def test_ghost_queue_all_s8_allows_short_only(self, mon):
        pm = mon['pm']
        self._seed(mon)
        pm._RECENTLY_GHOSTED.append(('B1', 'x', 1, 2, 3, 'LONG'))
        pm._RECENTLY_GHOSTED.append(('B2', 'x', 1, 2, 3, 'SHORT'))
        pm.monitor_all('S8')
        assert pm._RECENTLY_GHOSTED == [('B1', 'x', 1, 2, 3, 'LONG')]


class TestFinalSaveAndSummary:
    def test_save_always_called_even_without_close(self, mon):
        pm = mon['pm']
        pm._save({'AUSDT': {'entry': 5.0, 'side': 'SHORT', 'system': 'S8'}})
        mon['save_calls'].clear()
        pm.monitor_all('S8')
        # 2 次 save：第 1 次来自 _load meta_filtered 兜底回写；第 2 次为
        # monitor_all 终局快照（全量 all_positions）——均为生产冻结行为
        assert len(mon['save_calls']) == 2
        assert mon['save_calls'][-1] == {
            'AUSDT': {'entry': 5.0, 'side': 'SHORT', 'system': 'S8'}}

    def test_save_include_unfiltered_positions(self, mon):
        pm = mon['pm']
        pm._save({'AUSDT': {'entry': 5.0, 'side': 'SHORT', 'system': 'S8'},
                  'BUSDT': {'entry': 6.0, 'side': 'LONG'}})
        pm.monitor_all('S8')
        saved = mon['save_calls'][0]
        assert 'AUSDT' in saved and 'BUSDT' in saved   # 全量仓，不只 S8

    def test_summary_logged_only_when_closed(self, mon, monkeypatch):
        pm = mon['pm']
        pm._save({'AUSDT': {'entry': 5.0, 'side': 'SHORT'}})
        monkeypatch.setattr(pm, '_ghost_cleanup',
                            lambda p, f: [('AUSDT', 'x', 5.2)])
        fired = []
        monkeypatch.setattr(pm, 'log_position_summary',
                            lambda: fired.append(True))
        pm.monitor_all()                       # closed → summary
        assert fired == [True]
        fired.clear()
        monkeypatch.setattr(pm, '_ghost_cleanup', lambda p, f: [])
        pm.monitor_all()                       # 未平仓 → 无 summary
        assert fired == []
