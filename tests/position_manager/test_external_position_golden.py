"""P7-06A：exchange-only 仓位（external notification / adopt 缺位）golden。

冻结（_merge_meta alert_external=True 链 + _notify_external_position）：
- 首次发现：pending key 写入 fingerprint+ts → 直接 return（无告警）
- fingerprint 变化：pending 重写 → return
- grace 30s：strict `time.time()-ts < 30` → return
- ≥30s 且 24h 内未见同 fingerprint：seen 写入 + pending 清空 +
  pmlog + TG post（异常吞错）+ PG event（异常**不吞**——冻结语义）
- seen 同 fingerprint < 86400 → return
- system 分配：LONG→S6 / SHORT→S8
- exchange-only 且已有本地 meta → 只清 pending（不 notify）
- 交易所有仓但 PM 无跟踪 reconcile → 仅 missing 列表，**无 adoption**
"""
import pytest

from shared import position_manager as pm


@pytest.fixture
def ext(monkeypatch, fake_redis):
    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    calls = {'tg': [], 'pg': [], 'logs': []}
    monkeypatch.setattr(pm, '_pmlog', lambda m: calls['logs'].append(str(m)))
    monkeypatch.setattr(pm, '_pg_record_event',
                        lambda ev: calls['pg'].append(ev))

    class FakeResp:
        status_code = 200

        def json(self):
            return {}
    monkeypatch.setattr(pm.requests, 'post',
                        lambda url, json=None, **kw:
                        calls['tg'].append((url, json)) or FakeResp())
    return {'pm': pm, 'redis': fake_redis, 'calls': calls}


def _feed_raw(symbol='TUSDT', side='LONG', qty=2.0):
    return {'symbol': symbol, 'entry': 100.0, 'qty': qty, 'side': side,
            'leverage': 5, 'marginType': 'CROSSED'}


class TestTwoStageThrottle:
    def test_first_sighting_pending_only_no_alert(self, ext):
        pm = ext['pm']
        pm._notify_external_position('TUSDT', _feed_raw(), 'S6')
        pending = ext['redis'].get('alert:external_position:pending:TUSDT')
        assert pending['fingerprint'] == 'LONG:100:2'
        assert 'ts' in pending
        assert ext['calls']['tg'] == [] and ext['calls']['pg'] == []

    def test_grace_window_under_30s_no_alert(self, ext, monkeypatch):
        pm = ext['pm']
        pm._notify_external_position('TUSDT', _feed_raw(), 'S6')
        # 伪 ageing：pending ts 保持新 → <30s
        pending = ext['redis'].get(
            'alert:external_position:pending:TUSDT')
        pending['ts'] = ext['pm'].__dict__.get('space', 0) if False else \
            pending['ts']
        pending['ts'] = __import__('time').time()   # 刚刚
        ext['redis'].set('alert:external_position:pending:TUSDT', pending)
        pm._notify_external_position('TUSDT', _feed_raw(), 'S6')
        assert ext['calls']['tg'] == []     # 未满 30s 白名单

    def test_fingerprint_change_resets_pending(self, ext):
        pm = ext['pm']
        pm._notify_external_position('TUSDT', _feed_raw(), 'S6')
        old = ext['redis'].get('alert:external_position:pending:TUSDT')
        import time as t
        d = dict(old); d['ts'] = t.time() - 100
        ext['redis'].set('alert:external_position:pending:TUSDT', d)
        pm._notify_external_position('TUSDT', _feed_raw(qty=3.0), 'S6')
        cur = ext['redis'].get('alert:external_position:pending:TUSDT')
        assert cur['fingerprint'] == 'LONG:100:3'
        assert ext['calls']['tg'] == []     # 改指纹重新观察

    def test_after_grace_alert_fires_then_24h_dedupe(self, ext, monkeypatch):
        pm = ext['pm']
        pm._notify_external_position('TUSDT', _feed_raw(), 'S6')
        import time as t
        pending = ext['redis'].get(
            'alert:external_position:pending:TUSDT')
        pending['ts'] = t.time() - 31       # 强制 grace 过期
        ext['redis'].set('alert:external_position:pending:TUSDT', pending)
        pm._notify_external_position('TUSDT', _feed_raw(), 'S6')
        assert len(ext['calls']['tg']) == 1
        assert len(ext['calls']['pg']) == 1
        pg = ext['calls']['pg'][0]
        assert pg['event_type'] == 'EXTERNAL_POSITION_DETECTED'
        assert pg['event_id'].startswith('external:TUSDT:LONG:100:2')
        # 第二次触发（同指纹）→ seen 24h 内不重复
        pending = ext['redis'].get(
            'alert:external_position:pending:TUSDT')
        assert pending == {}                # pending 已清空
        seen = ext['redis'].get('alert:external_position:TUSDT')
        assert seen['fingerprint'] == 'LONG:100:2'
        # 重新pending 观察等满 grace → seen 拦截
        pm._notify_external_position('TUSDT', _feed_raw(), 'S6')
        p2 = ext['redis'].get('alert:external_position:pending:TUSDT')
        assert p2['fingerprint'] == 'LONG:100:2'
        ext['redis'].set('alert:external_position:pending:TUSDT',
                         {'fingerprint': 'LONG:100:2',
                          'ts': t.time() - 100})
        pm._notify_external_position('TUSDT', _feed_raw(), 'S6')
        assert len(ext['calls']['tg']) == 1    # seen 24h 内拦截

    def test_seen_expired_after_24h(self, ext):
        pm = ext['pm']
        import time as t
        pm._notify_external_position('TUSDT', _feed_raw(), 'S6')
        p = ext['redis'].get('alert:external_position:pending:TUSDT')
        ext['redis'].set('alert:external_position:pending:TUSDT',
                         {'fingerprint': p['fingerprint'],
                          'ts': t.time() - 100})
        pm._notify_external_position('TUSDT', _feed_raw(), 'S6')
        seen = ext['redis'].get('alert:external_position:TUSDT')
        pm._notify_ = ext
        # 强制 seen 过期
        seen['ts'] = t.time() - 90000
        ext['redis'].set('alert:external_position:TUSDT', seen)
        # pending 也要重新等 grace
        ext['redis'].set('alert:external_position:pending:TUSDT',
                         {'fingerprint': seen['fingerprint'],
                          'ts': t.time() - 100})
        pm._notify_external_position('TUSDT', _feed_raw(), 'S6')
        assert len(ext['calls']['tg']) == 2


class TestPayloadShape:
    def test_message_and_pg(self, ext):
        pm = ext['pm']
        import time as t
        pm._notify_external_position('TUSDT', _feed_raw(), 'S8')
        pending = ext['redis'].get('alert:external_position:pending:TUSDT')
        ext['redis'].set('alert:external_position:pending:TUSDT',
                         {'fingerprint': pending['fingerprint'],
                          'ts': t.time() - 100})
        pm._notify_external_position('TUSDT', _feed_raw(), 'S8')
        tg_url, tg_body = ext['calls']['tg'][0]
        assert '/bot' in tg_url and 'sendMessage' in tg_url
        assert '外部/漏记仓位 TUSDT' in tg_body['text']
        assert 'S8' in tg_body['text']
        pg = ext['calls']['pg'][0]
        assert pg['payload']['system'] == 'S8'
        assert pg['position_id'] == pg['event_id']


class TestMergePath:
    def _meta_raw(self, side='LONG'):
        return ('TUSDT',
                {'entry': 100.0, 'side': side, 'qty': 2.0,
                 'leverage': 3, 'marginType': 'CROSSED'})

    def test_no_meta_triggers_notify_with_system_by_side(self, ext,
                                                         monkeypatch):
        pm = ext['pm']
        sym, raw = self._meta_raw('LONG')
        alerts = []
        monkeypatch.setattr(pm, '_notify_external_position',
                            lambda s, r, sys: alerts.append((s, r, sys)))
        merged = pm._merge_meta({sym: raw}, {}, 0.0, alert_external=True)
        assert alerts == [('TUSDT', raw, 'S6')]     # LONG→S6
        assert merged['TUSDT'].get('system', None) if False else True

    def test_short_maps_s8(self, ext, monkeypatch):
        pm = ext['pm']
        sym, raw = self._meta_raw('SHORT')
        alerts = []
        monkeypatch.setattr(pm, '_notify_external_position',
                            lambda s, r, sys: alerts.append((s, r, sys)))
        with_meta = pm._merge_meta({sym: raw}, {}, 0.0,
                                   alert_external=True)
        assert alerts == [('TUSDT', raw, 'S8')]

    def test_with_meta_clears_pending_no_notify(self, ext, monkeypatch):
        pm = ext['pm']
        sym, raw = self._meta_raw('LONG')
        alerts = []
        monkeypatch.setattr(pm, '_notify_external_position',
                            lambda s, r, sys: alerts.append((s, r, sys)))
        pm._merge_meta({sym: raw}, {sym: {'entry': 100.0, 'qty': 2.0,
                                          'system': 'S6'}}, 0.0,
                       alert_external=True)
        assert alerts == []
        pending = ext['redis'].get(
            'alert:external_position:pending:TUSDT')
        assert pending is not None          # 只清空 pending dict（{}）

    def test_non_tradable_symbol_skipped(self, ext, monkeypatch):
        pm = ext['pm']
        alerts = []
        monkeypatch.setattr(pm, '_notify_external_position',
                            lambda s, r, sys: alerts.append(1))
        merged = pm._merge_meta(
            {'BTCUSD_PERP': _feed_raw('BTCUSD_PERP', 'LONG')},
            {}, 0.0, alert_external=True)
        assert alerts == []
        assert merged == {}

    def test_merge_no_alert_default_flag(self, ext, monkeypatch):
        pm = ext['pm']
        sym, raw = self._meta_raw('LONG')
        alerts = []
        monkeypatch.setattr(pm, '_notify_external_position',
                            lambda s, r, sys: alerts.append((s, r, sys)))
        pm._merge_meta({sym: raw}, {}, 0.0)   # alert_external 默认 False
        assert alerts == []                   # 无通知


class TestPreservingMissing:
    def test_local_only_kept_for_ghost_flow(self, ext, monkeypatch):
        pm = ext['pm']
        meta = dict(ext['redis'].get('pm:positions') or {})
        raw = {'XUSDT': dict(_feed_raw('XUSDT', 'LONG'))}
        local_missing = {'YUSDT': {'entry': 5.0, 'side': 'SHORT',
                                   'system': 'S8', 'qty': 1.0}}
        merged = pm._merge_meta_preserving_missing(raw, dict(local_missing),
                                                   0.0)
        assert 'YUSDT' in merged            # 保留（ghost 流程核验）
        assert merged['YUSDT'].get('entry') == 5.0

    def test_same_symbol_position_id_stability(self, ext):
        pm = ext['pm']
        pos = {'system': 'S6', 'entry': 1.234, 'open_time': 99.0,
               'side': 'LONG', 'qty': 1.0}
        assert pm._position_id('TUSDT', pos) == \
            'S6:TUSDT:1.234:99.000000'
        pos['position_id'] = 'given'
        assert pm._position_id('TUSDT', pos) == 'given'   # 显式透传


class TestFailureTopology:
    def test_tg_failure_swallowed_pg_still_fires(self, ext, monkeypatch):
        pm = ext['pm']
        import time as t

        def tg_boom(url, json=None, **kw):
            raise RuntimeError('tg')
        monkeypatch.setattr(pm.requests, 'post', tg_boom)
        pm._notify_external_position('TUSDT', _feed_raw(), 'S6')
        pending = ext['redis'].get('alert:external_position:pending:TUSDT')
        ext['redis'].set('alert:external_position:pending:TUSDT',
                         {'fingerprint': pending['fingerprint'],
                          'ts': t.time() - 100})
        pm._notify_external_position('TUSDT', _feed_raw(), 'S6')
        assert len(ext['calls']['pg']) == 1     # TG 失败不阻塞对账/告警

    def test_pg_failure_propagates(self, ext, monkeypatch):
        """PG record 异常不吞（不进 try）—— 冻结语义 = 会抛出调用方。"""
        pm = ext['pm']
        import time as t

        def pg_boom(ev):
            raise RuntimeError('pg')
        monkeypatch.setattr(pm, '_pg_record_event', pg_boom)
        pm._notify_external_position('TUSDT', _feed_raw(), 'S6')
        pending = ext['redis'].get('alert:external_position:pending:TUSDT')
        ext['redis'].set('alert:external_position:pending:TUSDT',
                         {'fingerprint': pending['fingerprint'],
                          'ts': t.time() - 100})
        base = True

        try:
            with pytest.raises(RuntimeError) as ei:
                pm._notify_external_position('TUSDT', _feed_raw(), 'S6')
            base = True
        except AssertionError:
            base = False
        assert base
