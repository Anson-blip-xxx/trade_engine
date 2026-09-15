"""P7-07A：Lifecycle 精确排序 golden（open / close / flat / partial spy）。

冻结（当前 HEAD 逐行，勿改）：
- open: recent-guard → leverage → marginType → MARKET order →
  `_algo_start_worker()` → `_algo_enqueue(sl)` → 构建位置 dict →
  `_load()` → duplicate check → `_save` → log → True
  （OBS-3：duplicate check 在 order 之后；PMB-30：dup 时 AlgoSL 已入队）
- close (full): recent-guard(force=False) → `_mark_closed`（marker-first）→
  risk 读#1 →（有实仓）→ round qty → ExecutionService close intent →
  risk 读#2 →（flat）→ `_cancel_all_algo` → pnl → PG CLOSE_ORDER_FILLED →
  log → record_trade(final_close=True) → pop → save → True
- exchange-flat close: risk 无实仓 → cancel_all → record('手动平仓' 相同 reason)
  → PG EXCHANGE_POSITION_FLAT → pop → save → True
- partial: ExecutionService partial intent → 校验 code →
  pos['qty'] = round(qty-close,4) → save → log（pnl_only_log）
"""
import pytest

from shared import position_manager as pm


@pytest.fixture
def op(monkeypatch, fake_redis):
    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
    monkeypatch.setattr(pm, '_was_closed_recently', lambda s: False)
    calls = {'fapi': [], 'enqueue': [], 'worker': [], 'save': [],
             'load': [], 'logs': [], 'mc': [], 'clr': [], 'exec': [],
             'rec': [], 'pg': []}

    def _save(p):
        calls['save'].append(dict(p))
    monkeypatch.setattr(pm, '_save', _save)
    monkeypatch.setattr(pm, '_algo_start_worker',
                        lambda: calls['worker'].append(1))
    monkeypatch.setattr(pm, '_algo_enqueue',
                        lambda sym, side, sl, qty:
                        calls['enqueue'].append((sym, side, sl, qty)))
    monkeypatch.setattr(pm, '_mark_closed', lambda s: calls['mc'].append(s))
    monkeypatch.setattr(pm, '_clear_closed_marker',
                        lambda s: calls['clr'].append(s))
    monkeypatch.setattr(pm, '_cancel_all_algo',
                        lambda s: calls.setdefault('cancel', []).append(s))
    monkeypatch.setattr(pm, '_pg_record_event',
                        lambda ev: calls['pg'].append(ev))
    monkeypatch.setattr(pm, '_pmlog', lambda m: calls['logs'].append(str(m)))
    return {'pm': pm, 'redis': fake_redis, 'calls': calls}


class TestOpenOrdering:
    def test_exact_sequence(self, op, monkeypatch):
        seen = []
        seq = []

        def fapi_post(path, params=None):
            seq.append(path)
            return {'orderId': 1, 'status': 'FILLED', 'executedQty': '100'}

        def load():
            seq.append('LOAD')
            seen.append('load')
            return {}

        save_calls = []

        def save(p):
            seq.append('SAVE')
            save_calls.append(p)
        pm = op['pm']
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, fapi_post, None, None, None, None, None, None))
        monkeypatch.setattr(pm, '_load', load)
        monkeypatch.setattr(pm, '_save_dict', None) if False else None
        monkeypatch.setattr(pm, '_save', save)
        monkeypatch.setattr(pm, '_algo_start_worker',
                            lambda: seq.append('WORKER'))
        monkeypatch.setattr(pm, '_algo_enqueue',
                            lambda *a: seq.append('ENQUEUE'))
        ok = pm.open_position('TUSDT', 'SHORT', 100.0, 10.0, 3, 95.0,
                              system='S6')
        assert ok is True
        # P9-03B：dup precheck 前移 —— load 在 side effects 之前
        assert seq == ['LOAD', '/fapi/v1/leverage', '/fapi/v1/marginType',
                       '/fapi/v1/order', 'WORKER', 'ENQUEUE', 'SAVE']
        assert save_calls[0]['TUSDT']['algo_sl_id'] is None

    def test_duplicate_precheck_rejects_before_side_effects(self, op,
                                                            monkeypatch):
        """P9-03B regression：local dup → **0 order / 0 enqueue**（OBS-3
        旧 expected 有意翻转），return 合同仍 True。"""
        pm = op['pm']
        seq = []
        monkeypatch.setattr(pm, '_algo_enqueue',
                            lambda *a: seq.append('ENQUEUE'))
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, lambda p, q=None: seq.append(p) or {'ok': 1},
            None, None, None, None, None, None))
        worker = []
        monkeypatch.setattr(pm, '_algo_start_worker',
                            lambda: worker.append(1))
        monkeypatch.setattr(pm, '_load', lambda: {
            'TUSDT': {'entry': 1.0, 'qty': 1.0}})
        ok = pm.open_position('TUSDT', 'SHORT', 100.0, 10.0, 3, 95.0)
        assert ok is True                        # return 合同不变
        assert seq.count('/fapi/v1/order') == 0      # 0 real order
        assert seq.count('ENQUEUE') == 0             # 0 orphan SL
        assert worker == []                      # worker 0 side effect
        assert op['calls']['save'] == []         # 0 save
        assert any('已在持仓中' in l for l in op['calls']['logs'])

    def test_recent_closed_guard_first_no_order(self, op, monkeypatch):
        pm = op['pm']
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s: True)
        fired = []
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, lambda p, q=None: fired.append(p), None, None,
            None, None, None, None))
        ok = pm.open_position('TUSDT', 'SHORT', 1.0, 1.0, 3, 0.99)
        assert ok is False and fired == []
        assert any('开仓拒绝' in l for l in op['calls']['logs'])


class TestCloseOrdering:
    def _close_env(self, op, monkeypatch, risk_pages, post_result=None):
        pm = op['pm']
        reads = {'n': 0}

        def fapi_get(path, params=None):
            reads['n'] += 1
            return risk_pages[min(reads['n'] - 1, len(risk_pages) - 1)]

        def fapi_post(path, params=None):
            if path == '/fapi/v1/order':
                return post_result or {'orderId': 9, 'status': 'FILLED',
                                       'executedQty': '10'}
            return {}
        gr = op['calls']
        seq = gr.setdefault('seq', [])

        def pg(ev):
            seq.append(('PG', ev['event_type']))
            op['calls']['pg'].append(ev)

        def cancel(s):
            seq.append('CANCEL')
            gr.setdefault('cancel', []).append(s)

        def rec(*a, **kw):
            seq.append(('REC', kw.get('final_close')))
            gr.setdefault('rec', []).append((a, kw))

        def mc(s):
            seq.append(('MARK', s))

        def save(p):
            seq.append('SAVE')
            gr['save'].append(dict(p))
        monkeypatch.setattr(pm, '_s6api', lambda: (
            fapi_get, fapi_post, None, None, None, None, None, rec))
        monkeypatch.setattr(pm, '_pg_record_event', pg)
        monkeypatch.setattr(pm, '_cancel_all_algo', cancel)
        monkeypatch.setattr(pm, '_mark_closed', mc)
        monkeypatch.setattr(pm, '_save', save)
        monkeypatch.setattr(pm, '_round_qty', lambda s, q: round(q, 6))

        class _Core:
            @staticmethod
            def close_intent(sym, side, qty):
                return ('close_intent', sym, side, qty)
        monkeypatch.setattr(pm._exec_core, 'close_intent',
                            staticmethod(
                            lambda s, side, q: ('ci', s, side, q)))

        def execute(intent):
            seq.append(('EXEC', intent[2]))
            class R:
                raw = post_result or {'orderId': 9, 'status': 'FILLED',
                                      'executedQty': str(abs(
                                          real_qty[0]))}
            return R()
        monkeypatch.setattr(pm, '_execution_service',
                            lambda: type('X', (), {'execute_order':
                                                   staticmethod(execute)})())
        real_qty = [None]
        return pm, seq, real_qty, reads

    def test_full_close_exact_sequence(self, op, monkeypatch):
        pm, seq, real_qty, reads = self._close_env(
            op, monkeypatch,
            risk_pages=[[{'symbol': 'AUSDT', 'positionAmt': '-10'}],
                        []])
        real_qty[0] = 10.0
        pos = _pos_children()
        positions = {'AUSDT': pos}
        ok = pm._close('AUSDT', pos, 2.0, '硬止损', positions)
        assert ok is True
        # 精确序：MARK → EXEC → CANCEL → PG → REC(final) → SAVE
        assert seq[0] == ('MARK', 'AUSDT')
        assert seq[1] == ('EXEC', 'SHORT')
        assert seq[2] == 'CANCEL'
        pg_types = [(s[0], s[1]) for s in seq if isinstance(s, tuple)
                    and s[0] == 'PG']
        assert pg_types == [('PG', 'CLOSE_ORDER_FILLED')]
        rec_flags = [s[1] for s in seq if isinstance(s, tuple)
                     and s[0] == 'REC']
        assert rec_flags == [True]
        assert seq[-1] == 'SAVE'

    def test_exchange_flat_exact_sequence(self, op, monkeypatch):
        """交易所已平（real_pos 缺失）→ cancel → record → PG FLAT →
        pop → save；**无执行调用**。"""
        pm, seq, real_qty, reads = self._close_env(
            op, monkeypatch,
            risk_pages=[[]])              # 首读即 flat
        pos = _pos_children()
        positions = {'AUSDT': pos}
        ok = pm._close('AUSDT', pos, 2.0, '追踪锁利', positions)
        assert ok is True
        assert seq[0] == ('MARK', 'AUSDT')
        # EXEC 不出现（无市价平仓调用）
        assert not any(isinstance(s, tuple) and s[0] == 'EXEC'
                       for s in seq)
        assert seq[1] == 'CANCEL'
        assert [('PG', t) for (_, t) in
                [s for s in seq if isinstance(s, tuple) and
                 s[0] == 'PG']] == [('PG', 'EXCHANGE_POSITION_FLAT')]
        assert [s[1] for s in seq if isinstance(s, tuple)
                and s[0] == 'REC'] == [True]
        assert seq[-1] == 'SAVE'
        assert positions == {}

    def test_close_partial_fill_sequence(self, op, monkeypatch):
        """执行成功但交易所仍有余仓 → REC(final_close=False) →
        PG CLOSE_ORDER_PARTIAL → save（qty 回写）→ return False；
        **无 cancel**（PMB-10：部分成交不删条件单）且 **marker 不 clear**。"""
        pm, seq, real_qty, reads = self._close_env(
            op, monkeypatch,
            risk_pages=[[{'symbol': 'AUSDT', 'positionAmt': '-10'}],
                        [{'symbol': 'AUSDT', 'positionAmt': '-6'}],
                        [{'symbol': 'AUSDT', 'positionAmt': '-6'}]])
        real_qty[0] = 4.0
        pos = _pos_children()
        positions = {'AUSDT': pos}
        ok = pm._close('AUSDT', pos, 2.1, '紧急止损', positions, force=True)
        assert ok is False
        assert seq[0] == ('MARK', 'AUSDT')
        assert seq[1] == ('EXEC', 'SHORT')
        assert 'CANCEL' not in seq       # partial 不取消条件单
        pg_types = [s[1] for s in seq if isinstance(s, tuple)
                    and s[0] == 'PG']
        assert pg_types == ['CLOSE_ORDER_PARTIAL']
        assert [s[1] for s in seq if isinstance(s, tuple)
                and s[0] == 'REC'] == [False]
        # 冻结序：partial 路径 save **在** record/PG **之前**
        # （与 full-close 相反——PMB-28）
        assert seq[2] == 'SAVE'
        assert seq[3] == ('REC', False)
        assert seq[4] == ('PG', 'CLOSE_ORDER_PARTIAL')
        assert positions['AUSDT']['qty'] == 6.0
        assert op['calls']['clr'] == ['AUSDT']   # P9-04B：partial 后 clear


def _pos_children():
    return {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0,
            'side': 'SHORT', 'system': 'S6', 'open_time': 111.0}
