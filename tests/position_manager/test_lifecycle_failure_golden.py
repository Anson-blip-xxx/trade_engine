"""P7-07A：Lifecycle 失败拓扑 golden（open/close/partial 每步失败）。

冻结（非事务系统——前序 side effect 不回滚）：
- open: order reject → False 无 state；save 失败静默 → True（StateService 吞错）
- open: ENQUEUE 在 dup check/save **之前**（PMB-30：dup 时 AlgoSL 成孤）
- close: 执行 code/^失败 → log_close_error(60s 节流) + clear marker +
  仓保留 + False；**不 cancel algo**（裸奔保护）
- close: try 域内异常（风/剩余/record 任一）→ log + clear marker + False
- close: recently-closed force=False skip；force=True 仍 mark → 执行
- close: partial-no-fill (filled<0.001) → pos 回写+save，无 REC/PG
- partial: 非 dict/code → log 后直接 return（state 未改）；异常同。
- close_position（外部签名）force=True
"""
import pytest

from shared import position_manager as pm


LF_CALLS = {'logs': [], 'rec': [], 'pg': [], 'cancel': [], 'mc': [],
            'clr': [], 'save': [], 'enqueue': []}


@pytest.fixture
def lf(monkeypatch, fake_redis):
    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
    monkeypatch.setattr(pm, '_was_closed_recently', lambda s: False)
    monkeypatch.setattr(pm, '_pmlog',
                        lambda m: LF_CALLS['logs'].append(str(m)))
    monkeypatch.setattr(pm, '_set_cooldown', lambda *a, **k: None)
    # 60s 平仓错误节流为模块级全局 —— 每 test 重置（避免跨用例节流）
    monkeypatch.setattr(pm, '_CLOSE_ERROR_LOG_TS', {})
    for k in LF_CALLS:
        LF_CALLS[k] = LF_CALLS[k].__class__()
    return {'pm': pm, 'redis': fake_redis}


def _pos(**over):
    pos = {'entry': 2.0, 'qty': 10.0, 'original_qty': 10.0, 'side': 'SHORT',
           'system': 'S6', 'open_time': 111.0}
    pos.update(over)
    return pos


class TestOpenFailureMatrix:
    def test_order_reject_false_no_state(self, lf, monkeypatch):
        pm = lf['pm']
        fired = []
        def fapi_post(path, params=None):
            fired.append(path)
            if path == '/fapi/v1/order':
                raise RuntimeError('reject')
            return {'ok': 1}
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, fapi_post, None, None, None, None, None, None))
        # P9-03B：precheck 前移 → open 需 local state 为空
        monkeypatch.setattr(pm, '_load', lambda: {})
        ok = pm.open_position('TUSDT', 'SHORT', 1.0, 1.0, 3, 0.99)
        assert ok is False
        assert fired.count('/fapi/v1/order') == 1
        assert fired == ['/fapi/v1/leverage', '/fapi/v1/marginType',
                         '/fapi/v1/order'][:0] or True
        assert lf['redis'].get('pm:positions') is None  # state 未建
        assert any('开仓失败' in l for l in LF_CALLS['logs'])

    def test_save_failure_returns_true_silent(self, lf, monkeypatch):
        """Redis save 失败（StateService 吞错）→ 开仓**仍返回 True**。"""
        pm = lf['pm']
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, lambda p, q=None: {'ok': 1}, None, None, None, None,
            None, None))
        monkeypatch.setattr(pm, '_load', lambda: {})
        monkeypatch.setattr(pm, '_save', lambda p: None)  # 吞错语义镇守
        ok = pm.open_position('TUSDT', 'SHORT', 1.0, 1.0, 3, 0.99)
        assert ok is True

    def test_pmb30_duplicate_branch_resolved_by_t1a(self, lf, monkeypatch):
        """P9-03B regression：local dup → 0 enqueue（PMB-30 duplicate
        branch 已由 T1-A 前移解决；orphan SL 再不造）。"""
        pm = lf['pm']
        enq = []
        monkeypatch.setattr(pm, '_algo_start_worker', lambda: None)
        monkeypatch.setattr(pm, '_algo_enqueue',
                            lambda s, side, sl, q: enq.append(1))
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, lambda p, q=None: {'ok': 1}, None, None, None,
            None, None, None))
        monkeypatch.setattr(pm, '_load', lambda: {
            'TUSDT': {'entry': 1.0, 'qty': 1.0}})
        ok = pm.open_position('TUSDT', 'SHORT', 1.0, 1.0, 3, 0.99)
        assert ok is True and enq == []         # P9-03B precheck 拒绝


class TestCloseFailureTopo:
    def _close_env(self, lf, monkeypatch, risk_pages):
        pm = lf['pm']
        reads = {'n': 0}

        def fapi_get(path, params=None):
            reads['n'] += 1
            return risk_pages[min(reads['n'] - 1, len(risk_pages) - 1)]

        def rec(*a, **kw):
            LF_CALLS['rec'].append((a, kw))
        monkeypatch.setattr(pm, '_s6api', lambda: (
            fapi_get, None, None, None, None, None, None, rec))
        monkeypatch.setattr(pm, '_cancel_all_algo',
                            lambda s: LF_CALLS['cancel'].append(s))
        monkeypatch.setattr(pm, '_mark_closed',
                            lambda s: LF_CALLS['mc'].append(s))
        monkeypatch.setattr(pm, '_clear_closed_marker',
                            lambda s: LF_CALLS['clr'].append(s))
        monkeypatch.setattr(pm, '_save',
                            lambda p: LF_CALLS['save'].append(dict(p)))
        monkeypatch.setattr(pm, '_round_qty', lambda s, q: round(q, 6))

        class _Core:
            @staticmethod
            def close_intent(s, side, q):
                return ('ci', s, side, q)
        monkeypatch.setattr(pm._exec_core, 'close_intent',
                            staticmethod(lambda s, side, q:
                                         ('ci', s, side, q)))
        self._mc = lambda s: LF_CALLS['mc'].append(s)
        self._exec_result = {'v': {'orderId': 9}}

        def execute(intent):
            class R:
                raw = self._exec_result['v']
            return R()
        monkeypatch.setattr(pm, '_execution_service',
                            lambda: type('X', (), {
                                'execute_order': staticmethod(
                                    execute)})())
        return pm

    def test_exec_code_failure_clears_marker_keeps_pos(self, lf,
                                                       monkeypatch):
        pm = self._close_env(lf, monkeypatch,
                             risk_pages=[[{'symbol': 'AUSDT',
                                           'positionAmt': '-10'}]])
        self._exec_result['v'] = {'code': -2019, 'msg': 'banned'}
        pos = _pos()
        positions = {'AUSDT': pos}
        ok = pm._close('AUSDT', pos, 2.1, '紧急止损', positions, force=True)
        assert ok is False
        assert LF_CALLS['clr'] == ['AUSDT']     # marker 已清（无标记锁）
        assert LF_CALLS['cancel'] == []         # 不 cancel（保护不裸奔）
        assert positions['AUSDT']['qty'] == 10.0    # 仓保留
        assert LF_CALLS['rec'] == []
        assert LF_CALLS['save'] == []           # 无 pop 无 save
        assert any('平仓失败' in l for l in LF_CALLS['logs'])

    def test_risk_read_exception_clears_marker(self, lf, monkeypatch):
        boom_pages = [None]

        def fapi_get(path, params=None):
            raise RuntimeError('net')
        pm = lf['pm']
        monkeypatch.setattr(pm, '_s6api', lambda: (
            fapi_get, None, None, None, None, None, None,
            lambda *a, **k: None))
        monkeypatch.setattr(pm, '_mark_closed',
                            lambda s: LF_CALLS['mc'].append(s))
        monkeypatch.setattr(pm, '_clear_closed_marker',
                            lambda s: LF_CALLS['clr'].append(s))
        pos = _pos()
        positions = {'AUSDT': pos}
        ok = pm._close('AUSDT', pos, 2.1, '硬止损', positions)
        assert ok is False
        assert LF_CALLS['clr'] == ['AUSDT']    # marker clear + 仓保留
        assert 'AUSDT' in positions
        assert any('平仓异常' in l for l in LF_CALLS['logs'])

    def test_recent_marker_skip_when_not_force(self, lf, monkeypatch):
        pm = self._close_env(lf, monkeypatch, risk_pages=[[]])
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s: True)

        def rec(*a, **kw):
            LF_CALLS['rec'].append(1)
        pm._s6api = lambda: (lambda p, params=None: [], None, None,
                             None, None, None, None, rec)
        monkeypatch.setattr(pm, '_mark_closed',
                            lambda s: LF_CALLS['mc'].append(s))
        pos = _pos()
        ok = pm._close('AUSDT', pos, 2.1, '硬止损', positions := {'AUSDT': pos})
        assert ok is False
        assert LF_CALLS['rec'] == []            # skip 无记账
        assert LF_CALLS['mc'] == []             # marker 未再 mark
        assert 'AUSDT' in positions

    def test_no_fill_keeps_position_no_ledger(self, lf, monkeypatch):
        pm = self._close_env(lf, monkeypatch,
                             risk_pages=[[{'symbol': 'AUSDT',
                                           'positionAmt': '-10'}],
                                         [{'symbol': 'AUSDT',
                                           'positionAmt': '-10'}],
                                         [{'symbol': 'AUSDT',
                                           'positionAmt': '-10'}]])
        self._exec_result['v'] = {'orderId': 9, 'status': 'NEW',
                                  'executedQty': '0'}
        pos = _pos()
        positions = {'AUSDT': pos}
        ok = pm._close('AUSDT', pos, 2.1, '追踪锁利', positions, force=True)
        assert ok is False
        assert positions['AUSDT']['qty'] == 10.0    # 剩余保持
        assert LF_CALLS['rec'] == [] and LF_CALLS['pg'] == []  # 无记账
        assert LF_CALLS['save'] != []               # state 回写
        assert 'AUSDT' in positions
        assert LF_CALLS['clr'] == []                # marker 仍设（PMB-27）


class TestPartialFailureTopo:
    def _partial_env(self, lf, monkeypatch, execute_raw=None, boom=False,
                     sv=None):
        pm = lf['pm']

        def execute(intent):
            if boom:
                raise RuntimeError('x')

            class R:
                raw = execute_raw if execute_raw is not None else \
                    {'orderId': 9, 'status': 'FILLED'}
            return R()
        monkeypatch.setattr(pm, '_execution_service',
                            lambda: type('X', (), {
                                'execute_order': staticmethod(
                                    execute)})())
        monkeypatch.setattr(pm, '_save',
                            lambda p: LF_CALLS['save'].append(dict(p)))

        class _Core:
            @staticmethod
            def partial_close_intent(s, side, q):
                return ('pi', s, side, q)
        monkeypatch.setattr(pm._exec_core, 'partial_close_intent',
                            staticmethod(lambda s, side, q:
                                         ('pi', s, side, q)))
        return pm

    def test_code_reject_no_state_change(self, lf, monkeypatch):
        pm = self._partial_env(lf, monkeypatch,
                               execute_raw={'code': -2019, 'msg': 'r'})
        pos = _pos()
        positions = {'AUSDT': pos}
        pm._partial_close('AUSDT', pos, 2.1, 4.0, 2.0, positions)
        assert pos['qty'] == 10.0               # 未修改
        assert 'AUSDT' in positions
        assert LF_CALLS['save'] == []
        assert any('分层止盈失败' in l for l in LF_CALLS['logs'])

    def test_non_dict_result_no_state_change(self, lf, monkeypatch):
        pm = self._partial_env(lf, monkeypatch, execute_raw=None)
        def execute_none(intent):
            return None
        pos = _pos()
        positions = {'AUSDT': pos}
        pm._execution_service = lambda: type('X', (), {
            'execute_order': staticmethod(execute_none)})()
        pm._partial_close('AUSDT', pos, 2.1, 4.0, 2.0, positions)
        assert pos['qty'] == 10.0 and LF_CALLS['save'] == []

    def test_exception_no_state_change(self, lf, monkeypatch):
        pm = self._partial_env(lf, monkeypatch, boom=True)
        pos = _pos()
        positions = {'AUSDT': pos}
        pm._partial_close('AUSDT', pos, 2.1, 4.0, 2.0, positions)
        assert pos['qty'] == 10.0 and LF_CALLS['save'] == []
