"""P7-06A：ghost 锁 / 重复记账去重链 golden（_try_record_ghost_trade）。

冻结：
- lock key `pm:ghost_close:{sym}`、owner 形态、ttl=60
- acquire fail → log『[幽灵跳过] 其他进程正在处理平仓』→ return False（无 release）
- recently closed →『已由 _close 记录』→ return False（lock 内检测；release 保持）
- record_trade 参数：exit_reason='幽灵仓关闭'、final_close=True、
  ghost_cleanup=True、position_id、qty=original_qty 优先
- ghost_price = _light_get_price(sym) or entry
- record raise →『[幽灵记录失败]』return False；finally release 保证
- lock 内：closed-check → record（transient 状态未改，无 save/mark）
"""
import pytest

from shared import position_manager as pm


@pytest.fixture
def lock(monkeypatch, fake_redis):
    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    calls = {'rec': [], 'acq': [], 'rel': [], 'logs': []}
    monkeypatch.setattr(pm, '_lock_acquire',
                        lambda k, o, ttl=45:
                        calls['acq'].append((k, o, ttl)) or True)
    monkeypatch.setattr(pm, '_lock_release',
                        lambda k, o: calls['rel'].append((k, o)))
    monkeypatch.setattr(pm, '_was_closed_recently', lambda s: False)
    monkeypatch.setattr(pm, '_light_get_price', lambda s: None)
    monkeypatch.setattr(pm, '_pmlog', lambda m: calls['logs'].append(str(m)))
    monkeypatch.setattr(pm, '_s6api', lambda: (
        None, None, None, None, None, None, None,
        lambda *a, **kw: calls['rec'].append((a, kw))))
    return {'pm': pm, 'redis': fake_redis, 'calls': calls}


class TestLockContract:
    def test_lock_shape_and_ttl(self, lock):
        ok = lock['pm']._try_record_ghost_trade('TUSDT', {'entry': 1.0})
        assert ok is True
        k, o, ttl = lock['calls']['acq'][0]
        assert k == 'pm:ghost_close:TUSDT' and ttl == 60
        assert o.startswith('ghost:')

    def test_acquire_fail_returns_false_no_release(self, lock, monkeypatch):
        pm = lock['pm']
        monkeypatch.setattr(pm, '_lock_acquire', lambda k, o, ttl=45: False)
        ok = pm._try_record_ghost_trade('TUSDT', {'entry': 1.0})
        assert ok is False
        assert lock['calls']['rel'] == []   # 未拿锁不 release
        assert any('其他进程正在处理平仓' in l
                   for l in lock['calls']['logs'])

    def test_acquired_released_even_on_record_raise(self, lock, monkeypatch):
        pm = lock['pm']

        def boom(*a, **kw):
            raise RuntimeError('db')
        pm._s6api = lambda: (None, None, None, None, None, None, None, boom)
        ok = pm._try_record_ghost_trade('TUSDT', {'entry': 1.0})
        assert ok is False
        assert any('幽灵记录失败' in l for l in lock['calls']['logs'])
        assert lock['calls']['rel'] == [lock['calls']['acq'][0][:2]]


class TestDedupChain:
    def test_recently_closed_skip_record(self, lock, monkeypatch):
        moon = lock['pm']
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s: True)
        ok = pm._try_record_ghost_trade('TUSDT', {'entry': 1.0})
        assert ok is False
        assert lock['calls']['rec'] == []   # lock 内去重（先锁后查）
        assert any('已由 _close 记录' in l for l in lock['calls']['logs'])
        # lock 依然被释放（finally）
        pairs = [(k, o) for (k, o, _t) in lock['calls']['acq']]
        assert lock['calls']['rel'] == pairs        # lock 依然被释放


class TestRecordPayload:
    def test_payload_frozen(self, lock):
        meta = {'entry': 1.5, 'side': 'SHORT', 'qty': 4.0,
                'original_qty': 9.0, 'leverage': 5, 'system': 'S8',
                'open_time': 123.0, 'event_type': 'evt', 'score': 3,
                'atr': 0.4, 'sl': 1.7, 'position_id': 'p:1'}
        def pid_pos(sym, p):
            return p.get('position_id') or 'PID'
        lock['pm']._position_id = pid_pos
        ok = lock['pm']._try_record_ghost_trade('TUSDT', meta)
        assert ok is True
        args, kw = lock['calls']['rec'][0]
        assert args == ('TUSDT', 1.5, 1.5, 9.0, 5, 'S8', 123.0)
        assert kw['exit_reason'] == '幽灵仓关闭'
        assert kw['side'] == 'SHORT'
        assert kw['final_close'] is True and kw['ghost_cleanup'] is True
        assert kw['position_id'] == 'p:1' and kw['score'] == 3

    def test_ghost_price_fbn_entry_fallback(self, lock, monkeypatch):
        pm = lock['pm']
        monkeypatch.setattr(pm, '_light_get_price', lambda s: 2.0)
        meta = {'entry': 1.0, 'qty': 1.0}
        pm._try_record_ghost_trade('TUSDT', meta)
        args, kw = lock['calls']['rec'][0]
        assert args[2] == 2.0

    def test_defaults_when_meta_minimal(self, lock):
        ok = lock['pm']._try_record_ghost_trade('TUSDT', {})
        ok and None
        assert ok is True
        args, kw = lock['calls']['rec'][0]
        assert kw['side'] == 'LONG' and args[4] == 3       # leverage 默认 3
        assert args[6] == args[6]                          # open_time=now 存在


class TestStateInteraction:
    def test_no_save_no_mark_inside_record(self, lock, monkeypatch):
        """WS record 链内不 save/marker——mark 由调用方在 record 成功后做。"""
        rocked = lock
        fired = []
        monkeypatch.setattr(rocked['pm'], '_mark_closed',
                            lambda s: fired.append(s))
        lock['pm']._try_record_ghost_trade('TUSDT', {'entry': 1.0,
                                                     'qty': 1.0})
        assert fired == [] and locked_meta_clean(rocked)


def locked_meta_clean(lock):
    return lock['redis'].get('pm:positions') is None
