"""P7-06A：ghost 队列契约 golden（FIFO / malformed / PMB-17 / restart）。

冻结（P7-05A 已部分覆盖，此处补齐队列级 contract）：
- backing：进程内 list（_RECENTLY_GHOSTED），无 Redis —— restart 清空
- production 无 producer（PMB-25）：代码中无 append 调用点，仅消费
- FIFO：A,B,C → A 先出（pop(0)）
- 每轮消费全部弹空（一击 while）
- matched symbol skip；malformed/short tuple → side=None → 直接消费（PMB-17）
- duplicate symbol：首个转 closed，后续 skip（closed_syms）
"""
import pytest

from shared import position_manager as pm


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


class TestQueueContract:
    def test_fifo_order(self, q):
        pm = q['pm']
        pm._save({'AUSDT': {'entry': 1.0}})
        pm._RECENTLY_GHOSTED.extend([
            ('A', 'x', 1, 2, 3, 'LONG'),
            ('B', 'x', 1, 2, 3, 'LONG'),
            ('C', 'x', 1, 2, 3, 'LONG')])
        out = pm.monitor_all()
        assert [c[0] for c in out] == ['A', 'B', 'C']   # FIFO

    def test_empty_after_consume(self, q):
        pm = q['pm']
        pm._save({'AUSDT': {'entry': 1.0}})
        pm._RECENTLY_GHOSTED.append(('A', 'x', 1, 2, 3, 'LONG'))
        pm.monitor_all()
        assert pm._RECENTLY_GHOSTED == []

    def test_duplicate_symbols_first_consumed_rest_skipped(self, q):
        pm = q['pm']
        pm._save({'AUSDT': {'entry': 1.0}})
        pm._RECENTLY_GHOSTED.extend([
            ('A', 'x', 1, 2, 3, 'LONG'),
            ('A', 'y', 1, 2, 3, 'LONG')])
        out = pm.monitor_all()
        sym_counts = sum(1 for c in out if c[0] == 'A')
        assert sym_counts == 1              # 第二入れず closed_syms skip
        assert pm._RECENTLY_GHOSTED == []

    def test_short_tuple_dropped_pmb17_fixed(self, q):
        """P9-06B regression：len<6 → log + drop（不再 bypass）。"""
        pm = q['pm']
        pm._save({'AUSDT': {'entry': 1.0, 'system': 'S8'}})
        pm._RECENTLY_GHOSTED.append(('B', 'x', 1, 2, 3))
        out = pm.monitor_all('S8')
        assert out == []                        # malformed drop
        assert pm._RECENTLY_GHOSTED == []

    def test_malformed_single_tuple_dropped(self, q):
        """P9-06B regression：`str` item（len<6）→ log + drop。"""
        pm = q['pm']
        pm._save({'AUSDT': {'entry': 1.0}})
        pm._RECENTLY_GHOSTED.append(('X',))
        out = pm.monitor_all()
        assert out == []                        # malformed drop

    def test_requeued_unmatched_kept_order(self, q):
        pm = q['pm']
        pm._save({'AUSDT': {'entry': 1.0, 'system': 'S6'}})
        pm._RECENTLY_GHOSTED.extend([
            ('B1', 'x', 1, 2, 3, 'SHORT'),
            ('B2', 'x', 1, 2, 3, 'SHORT')])
        pm.monitor_all('S6')
        # unmatched → 回堵保持原序
        assert pm._RECENTLY_GHOSTED == [
            ('B1', 'x', 1, 2, 3, 'SHORT'),
            ('B2', 'x', 1, 2, 3, 'SHORT')]

    def test_process_local_restart_reset(self, q):
        """_RECENTLY_GHOSTED 为内存 list：重启即空（进程内对象）。"""
        import importlib
        pm2 = q['pm']
        with_Queue = pm2._RECENTLY_GHOSTED
        with_Queue.append(('X', 'x', 1, 2, 3, 'SHORT'))
        assert pm2._RECENTLY_GHOSTED == with_Queue   # 同一对象
        # 模拟重启：重新 import 得到新 list（module init）
        import os, subprocess, sys, json, textwrap
        script = textwrap.dedent("""
            import sys; sys.path.insert(0, '.')
            import os, json; os.environ['PM_NO_WS']='1'
            from shared import position_manager as pm
            print(json.dumps(pm._RECENTLY_GHOSTED))
        """)
        env = dict(os.environ, PM_NO_WS='1')
        res = subprocess.run([sys.executable, '-c', script],
                             capture_output=True, text=True, env=env)
        assert res.stdout.strip().endswith('[]')
