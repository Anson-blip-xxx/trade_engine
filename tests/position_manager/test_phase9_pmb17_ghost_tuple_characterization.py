"""P9-06A: PMB-17 characterization (malformed ghost queue tuple / side=None bypass)."""
from __future__ import annotations

import pytest

import shared.position_manager as pm


@pytest.fixture
def q(monkeypatch, fake_redis):
    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    monkeypatch.setattr(pm, '_monitor_heartbeat_ts', 0.0)
    monkeypatch.setattr(pm, '_RECENTLY_GHOSTED', [])
    monkeypatch.setattr(pm, '_light_fapi_get', lambda p, params=None: [])
    monkeypatch.setattr(pm, '_light_fapi_delete', lambda p, params=None: {})
    monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
    monkeypatch.setattr(pm, '_ghost_cleanup', lambda p, f: [])
    monkeypatch.setattr(pm, '_monitor_one', lambda s, p, ap: None)
    return {'pm': pm, 'redis': fake_redis}


class TestQueueSchema:
    def test_frozen_side_unpack_parser(self):
        """frozen：consumer parser — `g_side = g[5] if len(g) >= 6 else None`
        + `if not system_filter or not g_side:` → bypass root. """
        src = open('position_monitoring/service.py').read()
        assert 'g_side = g[5] if len(g) >= 6 else None' in src
        assert 'if not system_filter or not g_side:' in src

    def test_producer_dead_no_append(self, fake_redis):
        """PMI-25：全仓库搜索 — `_RECENTLY_GHOSTED` 无 append 调用点
        （ DEAD queue — 仍是 consumer$LANG支持）。"""
        import subprocess
        r = subprocess.run(['grep', '-rn', '_RECENTLY_GHOSTED.append', '.'],
                           capture_output=True, text=True)
        hits = [l for l in r.stdout.splitlines()
                if l and ('.py' in l and 'test' not in l.lower())
                and '.pyc' not in l and '.git' not in l]
        assert not hits, hits      # producer dead（deprecated producer）


class TestValidTupleFiltering:
    def test_long_consumed_s6_filter_match(self, q):
        pm = q['pm']
        pm._save({'AUSDT': {'entry': 1.0, 'system': 'S6'}})
        pm._RECENTLY_GHOSTED.append(('B1', 'x', 1, 2, 3, 'LONG'))
        out = pm.monitor_all('S6')
        assert [c[0] for c in out] == ['B1']
        assert pm._RECENTLY_GHOSTED == []          # FIFO pop(0) 一击清空

    def test_short_consumed_s8_filter_match(self, q):
        pm = q['pm']
        pm._save({'AUSDT': {'entry': 1.0, 'system': 'S8'}})
        pm._RECENTLY_GHOSTED.append(('B2', 'x', 1, 2, 3, 'SHORT'))
        out = pm.monitor_all('S8')
        assert [c[0] for c in out] == ['B2']
        assert pm._RECENTLY_GHOSTED == []

    def test_long_not_matched_s8_kept(self, q):
        pm = q['pm']
        pm._save({'AUSDT': {'entry': 1.0, 'system': 'S6'}})
        pm._RECENTLY_GHOSTED.append(('B3', 'y', 1, 2, 3, 'LONG'))
        pm.monitor_all('S8')
        # unmatched side → requeued (FIFO preserved)
        assert pm._RECENTLY_GHOSTED == [('B3', 'y', 1, 2, 3, 'LONG')]


class TestMalformedMatrix:
    def test_short_tuple_side_none_bypassed_consumed(self, q):
        """PMB-17 core：len<6 → side=None → filter bypass → 被消费（frozen）。"""
        pm = q['pm']
        pm._save({'AUSDT': {'entry': 1.0, 'system': 'S8'}})
        pm._RECENTLY_GHOSTED.append(('B', 'x', 5.0, 5.0, 10.0))  # 5-tuple
        out = pm.monitor_all('S8')
        assert [c[0] for c in out] == ['B']          # side=None 绕过
        assert pm._RECENTLY_GHOSTED == []            # 一次性 pop；entry lost

    def test_side_none_filter_bypass_instruction(self, q):
        """root cause 冻结：`not g_side` 使 None 走 allowed 分支。"""
        src = open('position_monitoring/service.py').read()
        idx = src.index('g_side = g[5] if len(g) >= 6 else None')
        block = src[idx:idx + 300]
        assert 'not g_side' in block          # `not g_side` bypass frozen

    def test_string_item_bypassed_consumed_no_raise(self, q):
        """str item `'X'`：len=1 → `g[5]` no? — len('X')==1 not >=6 →
        `side=None` → consume（str item Frozen，spectrum）无 raise
        （frozen）。"""
        pm = q['pm']
        pm._save({'AUSDT': {'entry': 1.0}})
        pm._RECENTLY_GHOSTED.append(('X',))
        out = pm.monitor_all()
        assert out == [('X',)]                    # 正常 consume
        # queue 内 pops done → lost
        assert pm._RECENTLY_GHOSTED == []

    def test_no_requeue_semantics_frozen(self, q):
        """frozen：malformed entry consumption 后不 requeue —— lost。"""
        pm = q['pm']
        pm._save({'AUSDT': {'entry': 1.0}})
        pm._RECENTLY_GHOSTED.extend([('D', '', 0, 0, 0),
                                     ('C', 'y', 0, 0, 0, 'LONG')])
        pm.monitor_all('S6')
        # valid `LONG` 打 S6 filter → consumed；5-size tuple side=None → consumed
        assert pm._RECENTLY_GHOSTED == []        # malformed 丢失，无 retry

    def test_empty_tuple_indexerror_from_monitor_level(self, q):
        """empty tuple → g[0] → IndexError（monitor svc 层直接 raise），
        （frozen：不会自动 swallowed by consumer）—— monitor_all outer
        `set已吃exception` 对 per-symbol only，queue-consume zone 不 catch）。"""
        pm = q['pm']
        pm._save({'AUSDT': {'entry': 1.0}})
        pm._RECENTLY_GHOSTED.append(())
        with pytest.raises(IndexError):
            pm.monitor_all()                  # queue consumption 抛出（zero catch）
        pm._RECENTLY_GHOSTED.clear()
