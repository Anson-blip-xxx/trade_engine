"""P7-06A：local 有 / exchange 无（ghost 清理）golden。

冻结（P7-06A characterization，勿改）：
- sandbox → []
- record_trade 经 _s6api 第 8 位；取不到 → noop lambda
- puzzle: `_light_fapi_get` 非列表 → 完全跳过（宁漏不错）
- 交易所过滤 |positionAmt| < 0.001 → 视为无持仓（sym 进入清理集合）
- recently closed → 只 pop 不记录（无 record/document_trade）
- system filter mismatch → continue；**不 pop**（对方进程的仓保住）
- lock acquire fail → continue；**不 pop**（无锁不清理）
- _ghost_cleanup_one（lock 内）：pop → record(exit_reason='手动平仓',
  final_close=True, ghost_cleanup=True, position_id=_position_id) →
  _mark_closed → closed.append(6-tuple)
- record raise → 外层 catch『[幽灵检测异常]』→ **整个 loop 中止**
  （后续 symbol 不再清理——非 per-symbol isolation，PMB-23 冻结）
- ghost_price = _light_get_price(sym) or entry
"""
import pytest

from shared import position_manager as pm


@pytest.fixture
def gh(monkeypatch, fake_redis):
    monkeypatch.setattr(pm, '_rget', fake_redis.get)
    monkeypatch.setattr(pm, '_rset', fake_redis.set)
    monkeypatch.setattr('shared.redis_store.delete', fake_redis.delete)
    monkeypatch.setattr(pm, '_sandbox_active', lambda: False)
    monkeypatch.setattr(pm, '_light_fapi_get', lambda p, params=None: [])
    monkeypatch.setattr(pm, '_light_get_price', lambda s: None)
    monkeypatch.setattr(pm, '_was_closed_recently', lambda s: False)
    calls = {'rec': [], 'lock': [], 'mc': [], 'logs': []}
    monkeypatch.setattr(pm, '_lock_acquire',
                        lambda k, o, ttl=45:
                        calls['lock'].append(('acquire', k, o)) or True)
    monkeypatch.setattr(pm, '_lock_release',
                        lambda k, o: calls['lock'].append(('release', k, o)))
    monkeypatch.setattr(pm, '_mark_closed',
                        lambda s: calls['mc'].append(s))
    monkeypatch.setattr(pm, '_pmlog', lambda m: calls['logs'].append(str(m)))
    return {'pm': pm, 'redis': fake_redis, 'calls': calls}


class TestExchangeFiltering:
    def test_micro_amt_counts_as_gone(self, gh):
        """|positionAmt|=0.0005 < 0.001 → 交易所认为无仓 → 本地仓被当 ghost。"""
        monkeypatch = None
        gh['pm']._light_fapi_get = \
            lambda p, params=None: [{'symbol': 'AUSDT',
                                     'positionAmt': '0.0005'}]
        pm = gh['pm']
        positions = {'AUSDT': {'entry': 1.0, 'side': 'SHORT', 'qty': 2.0}}
        out = pm._ghost_cleanup(positions, '')
        assert out and out[0][0] == 'AUSDT'

    def test_zero_amt_counts_as_gone(self, gh):
        pm = gh['pm']
        pm._light_fapi_get = \
            lambda p, params=None: [{'symbol': 'AUSDT', 'positionAmt': '0'}]
        positions = {'AUSDT': {'entry': 1.0, 'side': 'SHORT', 'qty': 2.0}}
        out = pm._ghost_cleanup(positions, '')
        assert out and out[0][0] == 'AUSDT'

    def test_malformed_items_skipped(self, gh):
        pm = gh['pm']
        pm._light_fapi_get = lambda p, params=None: [
            'not-a-dict', None, {'no_symbol': 1},
            {'symbol': 'AUSDT', 'positionAmt': '2.0'}]
        positions = {'AUSDT': {'entry': 1.0, 'side': 'SHORT', 'qty': 2.0}}
        out = pm._ghost_cleanup(positions, '')
        assert out == []                   # AUSDT 仍在交易所 → 不清理

    def test_non_list_response_skip(self, gh):
        pm = gh['pm']
        pm._light_fapi_get = lambda p, params=None: {'err': 1}
        positions = {'AUSDT': {'entry': 1.0}}
        assert pm._ghost_cleanup(positions, '') == []
        assert positions == {'AUSDT': {'entry': 1.0}}   # state 未动

    def test_light_fapi_throw_caught(self, gh):
        pm = gh['pm']

        def boom(p, params=None):
            raise RuntimeError('net')
        pm._light_fapi_get = boom
        positions = {'AUSDT': {'entry': 1.0}}
        out = pm._ghost_cleanup(positions, '')
        assert out == []
        assert any('幽灵检测异常' in l for l in gh['calls']['logs'])


class TestRecentlyClosed:
    def test_recently_closed_pop_no_record(self, gh, monkeypatch):
        pm = gh['pm']
        monkeypatch.setattr(pm, '_was_closed_recently', lambda s: True)
        pm._light_fapi_get = lambda p, params=None: []
        positions = {'AUSDT': {'entry': 1.0, 'side': 'SHORT', 'qty': 2.0}}
        out = pm._ghost_cleanup(positions, '')
        assert out == []                    # 无记账
        assert 'AUSDT' not in positions    # 只 pop
        assert gh['calls']['rec'] == []
        assert gh['calls']['mc'] == []


class TestSystemFilter:
    def test_other_system_not_popped(self, gh):
        """system_filter 匹配失败 → continue 且 **不 pop**（保对方仓）。"""
        pm = gh['pm']
        pm._light_fapi_get = lambda p, params=None: []
        positions = {'AUSDT': {'entry': 1.0, 'side': 'SHORT',
                               'system': 'S6'}}
        out = pm._ghost_cleanup(positions, 'S8')
        assert out == []
        assert 'AUSDT' in positions         # 不 pop（多进程语义）

    def test_own_system_cleaned(self, gh):
        pm = gh['pm']
        pm._light_fapi_get = lambda p, params=None: []
        positions = {'AUSDT': {'entry': 1.0, 'side': 'SHORT',
                               'system': 'S8'}}
        out = pm._ghost_cleanup(positions, 'S8')
        assert out and out[0][0] == 'AUSDT'


class TestLocalOnlyRecord:
    def _mock_rec(self, monkeypatch):
        pm = None
        recs = []

        def rec(*a, **kw):
            recs.append((a, kw))
        return rec, recs

    def test_record_mark_seq(self, gh, monkeypatch):
        pm = gh['pm']
        pm._light_fapi_get = lambda p, params=None: []
        rec, recs = None, []
        def _rec(*a, **kw):
            recs.append((a, kw))
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, None, None, None, None, _rec))
        positions = {'AUSDT': {'entry': 1.0, 'side': 'SHORT', 'qty': 2.0,
                               'system': 'S8', 'leverage': 3,
                               'open_time': 111.0, 'sl': 99.0,
                               'algo_sl_id': 7, 'position_id': 'ext:1'}}
        out = pm._ghost_cleanup(positions, '')
        kw = recs[0][1]
        assert kw['exit_reason'] == '手动平仓'
        assert kw['final_close'] is True and kw['ghost_cleanup'] is True
        assert kw['position_id'] == 'ext:1'
        assert kw['side'] == 'SHORT' and kw['sl_price'] == 99.0
        assert kw['algo_sl_id'] == 7
        assert gh['calls']['mc'] == ['AUSDT']     # record 后 mark
        assert out == [('AUSDT', '手动平仓', 1.0, 1.0, 2.0, 'SHORT')]
        # pop 先于 record（lock 内 6 项动作顺序：pop→record→mark）
        assert positions == {}

    def test_ghost_price_falls_back_to_entry(self, gh, monkeypatch):
        pm = gh['pm']
        monkeypatch.setattr(pm, '_light_get_price', lambda s: 2.5)
        pm._light_fapi_get = lambda p, params=None: []
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, None, None, None, None, lambda *a, **k: None))
        positions = {'AUSDT': {'entry': 1.0, 'side': 'SHORT', 'qty': 2.0}}
        out = pm._ghost_cleanup(positions, '')
        assert out[0][2] == 2.5             # ghost_price = 现价

    def test_original_qty_preferred_over_qty(self, gh, monkeypatch):
        pm = gh['pm']
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, None, None, None, None, lambda *a, **k: None))
        monkeypatch.setattr(pm, '_position_id', lambda s, p: 'PID')
        positions = {'AUSDT': {'entry': 1.0, 'side': 'SHORT', 'qty': 1.0,
                               'original_qty': 5.0}}
        closed = []
        pm._lock_acquire = lambda *a, **k: True
        pm._ghost_cleanup_one('AUSDT', positions['AUSDT'], positions,
                              lambda *a, **k: None, closed)
        assert closed[0][4] == 5.0          # original_qty 优先


class TestLoopAbortsOnRecordFailure:
    def test_record_raise_kills_loop(self, gh, monkeypatch):
        """PMB-23：cleanup_one 内 raise → 整个 loop 中止（非 per-symbol）。"""
        pm = gh['pm']
        pm._light_fapi_get = lambda p, params=None: []
        # 第一仓 record raise；观察第二仓是否被处理
        raised = [False]

        def rec(*a, **kw):
            if a[0] == 'BUSDT':
                raise RuntimeError('x')
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, None, None, None, None, rec))
        positions = {'AUSDT': {'entry': 1.0, 'side': 'SHORT', 'system': 'S8',
                               'qty': 2.0},
                     'BUSDT': {'entry': 1.0, 'side': 'SHORT', 'system': 'S8',
                               'qty': 2.0},
                     'CUSDT': {'entry': 1.0, 'side': 'SHORT', 'system': 'S8',
                               'qty': 2.0}}
        out = pm._ghost_cleanup(positions, '')
        assert [c[0] for c in out] == ['AUSDT']   # A 成功后 B raise 中止余下
        assert 'BUSDT' not in positions and 'CUSDT' in positions
        # C 完全未处理（死循环序：pop 先于 record —— B 已 pop 却未记账/未 mark）
        assert 'BUSDT' not in gh['calls']['mc'] and \
            'CUSDT' not in gh['calls']['mc']
        # 主循环中止但锁仍然 released（finally 保证）
        rel_syms = [c[1] for c in gh['calls']['lock'] if c[0] == 'release']
        assert 'pm:ghost_close:BUSDT' in rel_syms and \
            'pm:ghost_close:CUSDT' not in rel_syms
        assert any('幽灵检测异常' in l for l in gh['calls']['logs'])

    def test_lock_acquired_before_cleanup(self, gh, monkeypatch):
        pm = gh['pm']
        pm._light_fapi_get = lambda p, params=None: []
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, None, None, None, None, lambda *a, **k: None))
        positions = {'AUSDT': {'entry': 1.0, 'side': 'SHORT', 'system': 'S8'}}
        pm._ghost_cleanup(positions, '')
        lock = [c for c in gh['calls']['lock']]
        assert lock and lock[0] == ('acquire', 'pm:ghost_close:AUSDT',
                                    lock[0][2])  # key/sym shape
        assert all(c[0] == 'acquire' for c in lock[:1]) and \
            'release' in lock[-1][0]

    def test_lock_fail_skips_and_keeps_pos(self, gh, monkeypatch):
        pm = gh['pm']
        monkeypatch.setattr(pm, '_lock_acquire',
                            lambda k, o, ttl=45: False)
        pm._light_fapi_get = lambda p, params=None: []
        positions = {'AUSDT': {'entry': 1.0, 'side': 'SHORT', 'system': 'S8'}}
        out = pm._ghost_cleanup(positions, '')
        assert out == [] and 'AUSDT' in positions   # 无锁不清理不 pop

    def test_release_called_even_on_record_raise(self, gh, monkeypatch):
        pm = gh['pm']
        monkeypatch.setattr(pm, '_lock_acquire', lambda k, o, ttl=45: True)
        pm._light_fapi_get = lambda p, params=None: []

        def boom(*a, **k):
            raise RuntimeError('r')
        monkeypatch.setattr(pm, '_ghost_cleanup_one', boom)
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, None, None, None, None, lambda *a, **k: None))
        positions = {'AUSDT': {'entry': 1.0, 'side': 'SHORT', 'system': 'S8'}}
        pm._ghost_cleanup(positions, '')
        assert ('release', 'pm:ghost_close:AUSDT', None) if False else \
            any(c[0] == 'release' and c[1] == 'pm:ghost_close:AUSDT'
                for c in gh['calls']['lock'])


class TestLogging:
    def test_summary_logged_when_cleaned(self, gh, monkeypatch):
        pm = gh['pm']
        monkeypatch.setattr(pm, '_light_fapi_get',
                            lambda p, params=None: [])
        monkeypatch.setattr(pm, '_s6api', lambda: (
            None, None, None, None, None, None, None, lambda *a, **k: None))
        monkeypatch.setattr(pm, '_position_id', lambda s, p: 'PID')
        positions = {'AUSDT': {'entry': 1.0, 'side': 'SHORT', 'system': 'S8'}}
        pm._ghost_cleanup(positions, '')
        assert any('幽灵清理完毕' in l and '1' in l
                   for l in gh['calls']['logs'])

    def test_no_summary_when_clean(self, gh):
        pm = gh['pm']
        pm._light_fapi_get = lambda p, params=None: [
            {'symbol': 'AUSDT', 'positionAmt': '2.0'}]
        positions = {'AUSDT': {'entry': 1.0, 'side': 'SHORT', 'qty': 2.0}}
        pm._ghost_cleanup(positions, '')
        assert not any('幽灵清理完毕' in l for l in gh['calls']['logs'])
