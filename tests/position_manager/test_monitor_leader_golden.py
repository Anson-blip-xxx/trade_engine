"""P7-05A：WS 领导选举 golden（lease/renew/fail-open + 连接循环门控）。

冻结（P7-01 扩展）：
- lease key 'ws:leader'（逐字）、TTL 45；_WS_INSTANCE 进程内常量
- 自持 → renew + True；无人持有 → acquire 竞争；他人持有 → False
- redis 异常 → True（fail-open，保实时监控不断线——P5-04/D4 家族语义）
- _ws_connect_loop 非领导者 → sleep 不连；leadership 失败 → 5s 退避
"""
import threading

import pytest

from shared import position_manager as pm
from shared import redis_store as rs


@pytest.fixture
def ws_env(monkeypatch, fake_redis):
    pm._WS_POSITIONS = {}
    pm._WS_LAST_UPDATE = 0.0
    calls = {'renew': [], 'acquire': [], 'owner': []}
    monkeypatch.setattr(pm, '_ws_listen_key', lambda: 'KEY123')
    return {'pm': pm, 'redis': fake_redis, 'calls': calls}


def _inject_leader(monkeypatch, owner):
    monkeypatch.setattr(rs, 'lock_owner', lambda key: owner)
    monkeypatch.setattr(rs, 'lock_renew',
                        lambda k, v, ttl: [] if False else True)
    monkeypatch.setattr(rs, 'lock_acquire',
                        lambda k, v, ttl: True)


class TestLeaseConstants:
    def test_lease_key_literal(self, ws_env):
        assert ws_env['pm']._WS_LEASE_KEY == 'ws:leader'

    def test_lease_ttl_45(self, ws_env):
        assert ws_env['pm']._WS_LEASE_TTL == 45

    def test_instance_is_pid_prefixed(self, ws_env):
        import os
        inst = ws_env['pm']._WS_INSTANCE
        assert inst.startswith(f'{os.getpid()}-')


class TestAmLeader:
    def test_self_holder_renews_true(self, ws_env, monkeypatch):
        calls = []
        monkeypatch.setattr(rs, 'lock_owner',
                            lambda k: ws_env['pm']._WS_INSTANCE)
        monkeypatch.setattr(rs, 'lock_renew',
                            lambda k, v, ttl:
                            calls.append((k, v, ttl)) or True)
        assert ws_env['pm']._ws_am_leader() is True
        assert calls == [(ws_env['pm']._WS_LEASE_KEY,
                          ws_env['pm']._WS_INSTANCE, 45)]

    def test_free_slot_acquires_true(self, ws_env, monkeypatch):
        calls = []
        monkeypatch.setattr(rs, 'lock_owner', lambda k: None)
        monkeypatch.setattr(rs, 'lock_acquire',
                            lambda k, v, ttl:
                            calls.append((k, v, ttl)) or True)
        assert ws_env['pm']._ws_am_leader() is True
        assert calls == [(ws_env['pm']._WS_LEASE_KEY,
                          ws_env['pm']._WS_INSTANCE, 45)]

    def test_other_owner_false_no_action(self, ws_env, monkeypatch):
        fired = []
        monkeypatch.setattr(rs, 'lock_owner', lambda k: 'other-pid-xyz')
        monkeypatch.setattr(rs, 'lock_renew',
                            lambda *a: fired.append('renew'))
        monkeypatch.setattr(rs, 'lock_acquire',
                            lambda *a: fired.append('acquire'))
        assert ws_env['pm']._ws_am_leader() is False
        assert fired == []                # 他人持有 → 不抢不续

    def test_redis_exception_fail_open_true(self, ws_env, monkeypatch):
        """锁定服务异常 → fail-open（可连接，保监控实时性）。"""
        def boom(k):
            raise RuntimeError('r')
        monkeypatch.setattr(rs, 'lock_owner', boom)
        monkeypatch.setattr(rs, 'lock_renew', boom)
        monkeypatch.setattr(rs, 'lock_acquire', boom)
        assert ws_env['pm']._ws_am_leader() is True


class TestConnectLoopGate:
    def test_non_leader_never_connects(self, ws_env, monkeypatch):
        """非领导者 → sleep 退避，不请求 listenkey / 不建 WS。"""
        fired = []
        monkeypatch.setattr(ws_env['pm'], '_ws_am_leader', lambda: False)
        monkeypatch.setattr(ws_env['pm'], '_ws_listen_key',
                            lambda: fired.append('key') or 'KEY123')
        monkeypatch.setattr(pm.time, 'sleep', lambda s: fired.append(('sleep', s)))

        def stop_after_first_sleep(sec):
            pm._WS_STOP = True
        monkeypatch.setattr(pm.time, 'sleep', stop_after_first_sleep)
        monkeypatch.setattr(pm, '_WS_STOP', False)
        pm_base = ws_env['pm']
        pm_base._WS_STOP_LOCAL = False
        import types
        t = threading.Thread(
            target=lambda: ws_env['pm']._ws_connect_loop(), daemon=True)
        t.start()
        t.join(timeout=3)
        assert fired == []                # 非领导者 → 一次都不连
        pm._WS_STOP = False


class TestConnectLoopLeader:
    def test_leader_requests_listenkey_and_builds_ws(self, monkeypatch, ws_env):
        fired = {}
        monkeypatch.setattr(ws_env['pm'], '_ws_am_leader', lambda: True)
        monkeypatch.setattr(ws_env['pm'], '_ws_listen_key', lambda: 'KEY123')
        monkeypatch.setattr(pm.time, 'sleep', lambda s: fired.setdefault('s', s))

        class FakeWS:
            def run_forever(self, **kw):
                fired['alphabet'] = kw
                pm._WS_STOP = True

        def fake_app(url, **kw):
            fired['url'] = url
            return FakeWS()
        import types
        monkeypatch.setitem(__import__('sys').modules, 'websocket',
                            types.SimpleNamespace(WebSocketApp=fake_app))
        monkeypatch.setattr(pm, '_WS_STOP', False)
        t = threading.Thread(target=lambda: ws_env['pm']._ws_connect_loop())
        t.start()
        t.join(timeout=3)
        ws_env['pm']._WS_STOP = False
        assert 'KEY123' in fired.get('url', '')
        assert fired['alphabet'] == {'ping_interval': 30, 'ping_timeout': 10}

    def test_empty_listen_key_skips_connect(self, monkeypatch, ws_env):
        fired = {}
        monkeypatch.setattr(ws_env['pm'], '_ws_am_leader', lambda: True)
        monkeypatch.setattr(ws_env['pm'], '_ws_listen_key', lambda: '')
        monkeypatch.setattr(pm.time, 'sleep', lambda s: fired.setdefault('s', s))

        def _sleep_then_stop(sec):
            fired['s'] = sec
            pm._WS_STOP = True
        monkeypatch.setattr(pm.time, 'sleep', _sleep_then_stop)
        monkeypatch.setattr(pm, '_WS_STOP', False)
        t = threading.Thread(target=lambda: ws_env['pm']._ws_connect_loop())
        t.start()
        t.join(timeout=3)
        pm._WS_STOP = False
        assert 's' in fired               # 连接跳过 → 5s 退避
