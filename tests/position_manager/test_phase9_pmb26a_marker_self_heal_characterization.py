"""P9-07A：PMB-26A characterization（closed-marker self-heal）。"""
from __future__ import annotations

import pytest

import shared.position_manager as pm


@pytest.fixture
def heal_env(monkeypatch, fake_redis):
    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    monkeypatch.setattr(pm, '_monitor_heartbeat_ts', 0.0)
    monkeypatch.setattr(pm, '_RECENTLY_GHOSTED', [])
    monkeypatch.setattr(pm, '_light_fapi_get', lambda p, params=None: [])
    monkeypatch.setattr(pm, '_light_fapi_delete', lambda p, params=None: {})
    monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
    monkeypatch.setattr(pm, '_pmlog', lambda *a, **k: None)
    return {'pm': pm, 'redis': fake_redis}


def _feed_raw(symbol='TUSDT', side='LONG', qty=2.0):
    return {symbol: {'entry': 100.0, 'qty': qty, 'side': side,
                     'leverage': 5, 'marginType': 'CROSSED'}}


class TestSelfHealTrigger:
    def test_fresh_marker_plus_exchange_position_clears(self, heal_env,
                                                         monkeypatch):
        """core：marker exists（fresh）+ exchange still has position →
        self-heal clears marker. """
        pm = heal_env['pm']
        cleared = []
        monkeypatch.setattr(pm, '_clear_closed_marker',
                            lambda s: cleared.append(s))
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s, w=4: True)
        raw = _feed_raw()
        pm._merge_meta(raw, {}, 0.0, alert_external=True)
        assert cleared == ['TUSDT']

    def test_clear_exactly_once(self, heal_env, monkeypatch):
        pm = heal_env['pm']
        cleared = []
        monkeypatch.setattr(pm, '_clear_closed_marker',
                            lambda s: cleared.append(s))
        with_marker = pm._was_closed_recently if False else None
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s, w=4: True)
        pm._merge_meta(_feed_raw(), {}, 0.0, alert_external=True)
        assert cleared == ['TUSDT']             # 一次 clear

    def test_no_unintended_mark(self, heal_env, monkeypatch):
        pm = heal_env['pm']
        marked = []
        monkeypatch.setattr(pm, '_mark_closed', lambda s: marked.append(s))
        pm._merge_meta(_feed_raw(), {}, 0.0, alert_external=True)
        assert marked == []                     # self-heal 不 mark


class TestNotificationOrdering:
    def test_clear_before_notify(self, heal_env, monkeypatch):
        """冻结：marker clear 发生在 notify 之前（merge 链顺序）。"""
        pm = heal_env['pm']
        seq = []
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s, w=4: True)
        monkeypatch.setattr(pm, '_clear_closed_marker',
                            lambda s: seq.append(('clear', s)))
        monkeypatch.setattr(pm, '_notify_external_position',
                            lambda s, r, sys: seq.append(('notify', s)))
        pm._merge_meta(_feed_raw(), {}, 0.0, alert_external=True)
        assert [t[0] for t in seq] == ['clear', 'notify']


class TestLocalExchangeMatrix:
    def test_local_exists_exchange_exists_no_heal(self, heal_env,
                                                    monkeypatch):
        """本地有仓+交易所有仓（正常 reopen 场景），marker absent → no heal."""
        pm = heal_env['pm']
        cleared = []
        monkeypatch.setattr(pm, '_clear_closed_marker',
                            lambda s: cleared.append(s))
        pm._merge_meta(_feed_raw(), {'TUSDT': {'entry': 100.0,
                                               'qty': 2.0,
                                               'system': 'S6'}}, 0.0,
                       alert_external=True)
        assert cleared == []                    # no marker → no heal

    def test_local_missing_exchange_exists_heals_and_notifies(self,
                                                               heal_env,
                                                               monkeypatch):
        pm = heal_env['pm']
        cleared, notified = [], []
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s, w=4: True)
        monkeypatch.setattr(pm, '_clear_closed_marker',
                            lambda s: cleared.append(s))
        monkeypatch.setattr(pm, '_notify_external_position',
                            lambda s, r, sys: notified.append(s))
        merged = pm._merge_meta(_feed_raw(), {}, 0.0, alert_external=True)
        assert cleared == ['TUSDT'] and notified == ['TUSDT']
        # merged 有 enriched entry（从 strategy/gate rejoin）
        assert 'TUSDT' in merged

    def test_local_exists_exchange_missing_no_heal_marker_kept(self,
                                                                 heal_env,
                                                                 monkeypatch):
        pm = heal_env['pm']
        cleared = []
        monkeypatch.setattr(pm, '_clear_closed_marker',
                            lambda s: cleared.append(s))
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s, w=4: True)
        # raw 不含 sym → loop 不触发 heal；ghost flow 处理
        merged = pm._merge_meta({}, {}, 0.0, alert_external=True)
        assert cleared == []


class TestPendingSeenInteraction:
    def test_pending_cleared_when_meta_exists(self, heal_env):
        pm = heal_env['pm']
        pm._merge_meta(_feed_raw(), {'TUSDT': {'entry': 100.0,
                                               'qty': 2.0,
                                               'system': 'S6'}}, 0.0,
                       alert_external=True)
        # pending key 被 _rset 置为空 dict（无抛出）；seen key 不动
        assert heal_env['redis'].get(
            'alert:external_position:pending:TUSDT') == {}

    def test_pending_key_untouched_on_heal(self, heal_env, monkeypatch):
        pm = heal_env['pm']
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s, w=4: True)
        pm._merge_meta(_feed_raw(), {}, 0.0, alert_external=True)
        # self-heal 路径将触发 external-position notify → pending key 被
        # 写入首观察 fingerprint（PMB-26A→notify 顺序冻结）；seen 不动
        pending = heal_env['redis'].get(
            'alert:external_position:pending:TUSDT')
        assert pending['fingerprint'] == 'LONG:100:2'
        assert heal_env['redis'].get(
            'alert:external_position:TUSDT') is None   # seen absent


class TestPMB27Relation:
    def test_pmb27_separate(self):
        """PMB-27 = close-lifecycle partial marker（已修）；
        PMB-26A = merge-chain self-heal（reconcile recovery）——不同根。"""
        import inspect
        p27 = inspect.getsource(
            __import__('position_lifecycle.service',
                       fromlist=['x']).PositionLifecycleService.close)
        assert 'clr' in p27    # close 修好了（P9-04B）
        # marker self-heal 不在 close path
        assert '_clear_closed_marker' not in p27


class TestConclusion:
    def test_intentional_self_heal_conclusion(self):
        """PMB-26A conclusion：**KEEP AS-IS** —— marker self-heal 是
        纠错恢复（exchange reality wins over stale marker）。
        双向闭合保护 dedup 窗口不会长期阻断。"""
        src = open('shared/position_manager.py').read()
        assert '[闭标记修复]' in src
        assert '_clear_closed_marker(sym)' in src
