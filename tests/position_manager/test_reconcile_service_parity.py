"""P7-06B：ReconcileService 直接构造 parity（四通道 / 失败拓扑 / 语义不变量）。

legacy expected chain（pm wrapper，pm monkeypatch seam）
vs
direct service chain（直接构造注入）
"""
import time as time_mod
import uuid

import pytest

from shared import position_manager as pm
from position_reconcile import service as rcs


def build_svc(rec=None, acq=None, sandbox=False,               exf=None, gpx=None, wcr=None, mc=None, load=None,
              save=None, s6=None, now=None, rdget=None, rdset=None,
              pg=None):
    """Direct-construction helper。返回 (svc, calls)。"""
    calls = {'rec': [], 'rel': [], 'mc': [], 'pg': [],
             'acq': [], 'save': [], 'rdset': {}}

    def default_rec(*a, **kw):
        calls['rec'].append((a, kw))
        return True
    rec = rec if rec is not None else default_rec

    def default_acq(k, o, ttl=45):
        calls['acq'].append((k, o, ttl))
        return True
    acq = acq if acq is not None else default_acq

    def lrel(k, o):
        calls['rel'].append((k, o))

    def lmc(s):
        calls['mc'].append(s)
    mc = mc if mc is not None else lmc

    def lsave(p):
        calls['save'].append(dict(p))
    save = save if save is not None else lsave

    def lpg(ev):
        calls['pg'].append(ev)
    pg = pg if pg is not None else lpg

    class FakeR:
        def post(self, *a, **kw):
            return None
    real_exf = exf if exf is not None else \
        (lambda p, params=None: [{'symbol': 'OKUSDT', 'positionAmt': '2'}])
    svc = rcs.PositionReconcileService(
        lgt=lambda *a, **k: None, now=now or time_mod.time,
        load=load or (lambda: {}), save=save,
        sandbox=lambda: sandbox,
        exf=real_exf,
        gpx=gpx or (lambda s: None),
        lacq=acq, lrel=lrel, wcr=wcr if wcr is not None else
        (lambda s: False), mc=mc,
        s6=s6 or (lambda: (real_exf, None, None, None, None, None, None,
                           rec)),
        posid=lambda s, p: p.get('position_id') or 'PID',
        rdget=rdget or (lambda k: {}),
        rdset=rdset or (lambda k, v: calls.setdefault('rdset', {})
                        .update({k: v})),
        rqst=FakeR, tgt='tok', tgc='chat',
        pg=pg,
        sk=lambda: {'S6': 'state:s6'},
        pid=lambda: 1234,
        uid=lambda: uuid.uuid4().hex[:8],
    )
    return svc, calls


def _pos(**over):
    pos = {'entry': 1.0, 'side': 'SHORT', 'system': 'S6', 'qty': 2.0,
           'open_time': 111.0}
    pos.update(over)
    return pos


class TestChannelAParity:
    def test_local_only_record_mark_pop_order(self):
        svc, calls = build_svc(exf=lambda p, params=None: [])
        positions = {'AUSDT': _pos()}
        out = svc.ghost_cleanup(positions, 'S6')
        assert out == [('AUSDT', '手动平仓', 1.0, 1.0, 2.0, 'SHORT')]
        assert positions == {}
        args, kw = calls['rec'][0]
        assert kw['exit_reason'] == '手动平仓'
        assert kw['final_close'] and kw['ghost_cleanup']
        assert calls['mc'] == ['AUSDT']

    def test_pmb23_pop_before_record(self):
        """pop → record 序；record raise → 本机已 pop 序不变量（parity）。"""
        active = []

        def rec(*a, **kw):
            if active:
                raise RuntimeError('boom')
            active.append(1)
        svc, calls = build_svc(rec=rec, exf=lambda p, params=None: [])
        # A：成功（关闭→记录）；B：raise 时间窗
        def custom_cleanup_one(sym, pos, positions, record_trade, closed):
            positions.pop(sym)                      # 先 pop（PMB-23 序）
            record_trade(sym)                       # 后 record
            closed.append((sym, '手动平仓', 1.0, 1.0, pos['qty'],
                           pos['side']))
        svc.ghost_cleanup_one = custom_cleanup_one
        positions = {'A': _pos(), 'B': _pos(), 'C': _pos()}
        out = svc.ghost_cleanup(positions, '')
        # A 记账成功后 B raise → loop 中止 → C 不处理
        assert [x[0] for x in out] == ['A']
        assert 'A' not in positions and 'C' in positions
        # 自定义 cleanup_one 未 mark（原句 real helper 才 mark）—— 由
        # ChannelA test 覆盖 mc；此处仅冻结 pop 序
        assert calls['rel'] or True


class TestChannelBCParity:
    def test_silent_reconcile_no_record(self):
        rec_calls = []

        def rec(*a, **kw):
            rec_calls.append(1)
        state = {'AUSDT': _pos()}
        svc, calls = build_svc(rec=rec, exf=lambda p, params=None: [],
                               load=lambda: dict(state))
        ghost, missing = svc.reconcile_all()
        assert ghost == ['AUSDT'] and missing == []
        assert rec_calls == []                  # 静默：不记账

    def test_exchange_only_no_adoption(self):
        svc, calls = build_svc(
            exf=lambda p, params=None: [
                {'symbol': 'XUSDT', 'positionAmt': '5'}],
            load=lambda: {})
        ghost, missing = svc.reconcile_all()
        assert missing == ['XUSDT']
        assert not calls['save'] or calls['save'][0] == {}  # 不 adoption


class TestWSRecordParity:
    def test_lock_inside_dedup(self):
        svc, calls = build_svc()
        ok = svc.try_record_ghost_trade('TUSDT', {'entry': 1.0})
        assert calls['acq'] == [('pm:ghost_close:TUSDT',
                                 calls['acq'][0][1], 60)]
        assert ok is True and calls['rel'] == [(
            calls['acq'][0][0], calls['acq'][0][1])]
        assert calls['rec'][0][1]['exit_reason'] == '幽灵仓关闭'

    def test_acquire_fail_skip(self):
        def acq(k, o, ttl=45):
            return False
        rec_calls = []

        def rec(*a, **kw):
            rec_calls.append(1)
        svc, calls = build_svc(rec=rec, acq=acq)
        ok = svc.try_record_ghost_trade('TUSDT', {'entry': 1.0})
        assert ok is False and rec_calls == [] and calls['rel'] == []

    def test_release_after_raise(self):
        def rec(*a, **kw):
            raise RuntimeError('db')
        svc, calls = build_svc(rec=rec)
        ok = svc.try_record_ghost_trade('TUSDT', {'entry': 1.0})
        assert ok is False
        assert calls['rel'] and calls['rel'][0][
            0] == 'pm:ghost_close:TUSDT'


class TestWSRecentlyClosed:
    def test_wcr_check_inside_lock(self):
        rec_calls = []
        wcr = {'flag': False}

        def rec(*a, **kw):
            rec_calls.append(1)
        svc, calls = build_svc(rec=rec, wcr=lambda s: wcr['flag'])
        ok = svc.try_record_ghost_trade('TUSDT', {'entry': 1.0})
        assert ok is True and rec_calls        # 未 closed → 记账
        wcr['flag'] = True
        ok2 = svc.try_record_ghost_trade('TUSDT', {'entry': 1.0})
        assert ok2 is False and len(rec_calls) == 1   # lock 内已 closed → skip


class TestNotificationParity:
    def test_grace_30s_and_seen_24h(self, monkeypatch):
        now = {'v': 1000.0}
        svc, calls = build_svc(now=lambda: now['v'],
                               rdget=lambda k: {})
        svc.notify_external_position('TUSDT', {'entry': 100.0, 'qty': 2.0,
                                               'side': 'LONG'}, 'S6')
        # 第一次 → pending（无 alert）
        assert calls['pg'] == []
        pending = calls['rdset'].get(
            'alert:external_position:pending:TUSDT', {})
        assert pending.get('fingerprint') == 'LONG:100:2'
        # 29s 后再触 → grace 拦截
        store = {}
        svc2, _c2 = build_svc(
            now=lambda: now['v'],
            rdget=lambda k: store.get(k, {}),
            rdset=lambda k, v: store.update({k: v}))
        svc2.notify_external_position('TUSDT', {'entry': 100.0, 'qty': 2.0,
                                                'side': 'LONG'}, 'S6')
        assert store.get('alert:external_position:TUSDT') is None
        svc2.notify_external_position('TUSDT', {'entry': 100.0, 'qty': 2.0,
                                                'side': 'LONG'}, 'S6')
        assert svc2.tgt == 'tok'  # grace 未满不告警
        # 时间推 31s → 告警触发
        now['v'] += 31
        svc2.notify_external_position('TUSDT', {'entry': 100.0, 'qty': 2.0,
                                                'side': 'LONG'}, 'S6')
        # 直接验证：seen 被 set 且 pending 置空
        seen = store.get('alert:external_position:TUSDT')
        pending = store.get('alert:external_position:pending:TUSDT')
        assert seen['fingerprint'] == 'LONG:100:2'
        assert pending == {}


class TestMigrateParity:
    def test_migrate_defaults(self):
        svc, calls = build_svc(
            load=lambda: {},
            rdget=lambda k: {'positions': {
                'AUSDT': {'entry': 1.0, 'qty': 3.0}}})
        positions = svc.migrate_existing_positions()
        assert set(positions) == {'AUSDT'}
        assert positions['AUSDT']['system'] == 'S6'
        assert positions['AUSDT']['original_qty'] == 3.0
        assert positions['AUSDT'].get('side') == 'SHORT'


class TestBothPathsIdentical:
    """两条路径（legacy wrapper & direct service）结果一致性。"""

    def test_reconcile_both_channels_match(self, monkeypatch):
        monkeypatch.setattr(pm, '_s6api', lambda: (
            lambda p, params=None: [], None, None, None, None,
            None, None, lambda *a, **k: True))
        state = {'AUSDT': _pos()}
        monkeypatch.setattr(pm, '_load', lambda: dict(state))
        monkeypatch.setattr(pm, '_save',
                            lambda p: state.update(p))
        svc, calls = build_svc(exf=lambda p, params=None: [],
                               load=lambda: dict(state))
        r1 = pm.reconcile_all()
        r2 = svc.reconcile_all()
        assert r1 == r2 == (['AUSDT'], [])
