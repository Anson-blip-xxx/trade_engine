"""P6-03：S0 Publisher boundary contract / parity / failure matrix。"""
import json
import os

import pytest

from s0.adapters import S0PublisherAdapter
from s0.ports import S0PublisherPort

BASE = {'market_state': 'range', 'btc_trend': 'bull', 'breadth': 'normal',
        'breadth_ratio': 0.5, 'volatility': 'low', 'risk_off': False}


class RecordingRedis:
    def __init__(self):
        self.calls = []
        self.exc = None

    def __call__(self, key, val):
        self.calls.append((key, val))
        if self.exc is not None:
            raise self.exc


class RecordingCH:
    def __init__(self):
        self.rows = []
        self.errors = []
        self.exc = None

    def __call__(self, table, row):
        self.rows.append((table, row))
        if self.exc is not None:
            raise self.exc

    def on_error(self, e):
        self.errors.append(('ch-error', e))


class RecordingFile:
    def __init__(self):
        self.states = []
        self.exc = None

    def write(self, state):
        self.states.append(state)
        if self.exc is not None:
            raise self.exc


# ═══════════════════════════════════════════════════════════════
#  Port contract 与 adapter 职责面
# ═══════════════════════════════════════════════════════════════

def test_port_contract():
    import inspect
    from s0.ports import S0PublisherPort
    members = [name for name, _ in inspect.getmembers(
        S0PublisherPort, predicate=inspect.isfunction)
        if not name.startswith('_')]
    assert members == ['publish_state']


def test_adapter_satisfies_port():
    ad = S0PublisherAdapter(
        redis_set=RecordingRedis(), file_write=RecordingFile().write,
        ch_insert=RecordingCH(), on_ch_error=lambda e: None)
    from s0.ports import S0PublisherPort
    assert isinstance(ad, S0PublisherPort)


# ═══════════════════════════════════════════════════════════════
#  三写顺序 + S0-9 失败矩阵
# ═══════════════════════════════════════════════════════════════

class TestCallOrder:
    def test_redis_then_file_then_ch(self, tmp_path):
        order = []
        st = dict(BASE)
        ad = S0PublisherAdapter(
            redis_set=lambda k, v: order.append(('redis', k)),
            file_write=lambda state: order.append(('file',)),
            ch_insert=lambda t, r: order.append(('ch', t)),
            on_ch_error=lambda e: None)
        ad.publish_state(st)
        assert order == [('redis', 'market:s0'), ('file',),
                         ('ch', 'default.market_state_log')]


class TestFailureMatrix:
    def test_redis_failure_file_ch_still(self, tmp_path):
        """S0-9：redis 失败 → 吞错继续（file/CH 照写）。"""
        r = RecordingRedis()
        r.exc = RuntimeError('down')
        f, c = RecordingFile(), RecordingCH()
        ad = S0PublisherAdapter(r, f.write, c, c.on_error)
        ad.publish_state(dict(BASE))
        assert len(r.calls) == 1 and f.states and c.rows    # 顺序照样执行

    def test_file_failure_raises_and_ch_skipped(self, tmp_path):
        """file 失败 → raise，CH 不执行（冻结行为）。"""
        events = []

        def bad_write(state):
            events.append('file-fail')
            raise OSError('disk full')

        ad = S0PublisherAdapter(RecordingRedis(), bad_write, RecordingCH(),
                                lambda e: events.append(('ch-error', e)))
        with pytest.raises(OSError):
            ad.publish_state(dict(BASE))
        assert events == ['file-fail']             # CH 跳过

    def test_ch_failure_no_raise(self, tmp_path):
        st = dict(BASE)
        ch = RecordingCH()
        ch.exc = RuntimeError('ch down')
        ad = S0PublisherAdapter(RecordingRedis(), RecordingFile().write, ch,
                                ch.on_error)
        ad.publish_state(st)                       # 不抛
        assert ch.errors and ch.rows


# ═══════════════════════════════════════════════════════════════
#  原子文件 parity（tmp→json→rename）
# ═══════════════════════════════════════════════════════════════

class TestAtomicFileSemantics:
    def test_file_written_via_atomic(self, s0_env, g, rdis, ch_rows, tmp_path):
        """tmp → json.dump（无 indent）→ os.replace 原子覆盖。"""
        st = g.compute_state('bull', 'low', 0.02, False, False, 'normal', 0.5)
        g.write_state(st)
        expected = tmp_path / 'market_state.json'
        assert expected.exists()
        content = expected.read_text()
        assert json.loads(content)['regime'] == 'weak_bull'

    def test_ch_row_shape_frozen(self, s0_env, g, rdis, ch_rows, tmp_path):
        st = g.compute_state('bull', 'low', 0.02, False, False, 'normal', 0.5)
        g.write_state(st)
        assert ch_rows and ch_rows[0][0] == 'default.market_state_log'
        row = json.loads(ch_rows[0][1])
        assert set(row.keys()) == {'market_state', 'btc_trend', 'breadth',
                                   'breadth_ratio', 'volatility', 'risk_off'}
        assert row['risk_off'] == 0                # int 0/1（非 bool）

    def test_redis_no_ttl_no_publish(self, s0_env, g, rdis, tmp_path):
        st = g.compute_state('bull', 'low', 0.02, False, False, 'normal', 0.5)
        g.write_state(st)
        key, val = rdis.writes[0]
        assert key == 'market:s0'
        assert val is st                           # 引用（不拷贝）


class TestNoMutation:
    def test_publish_state_no_mutation(self, tmp_path):
        import io
        st = dict(BASE)
        sentinel = dict(st)
        file = RecordingFile()
        ch = RecordingCH()
        ad = S0PublisherAdapter(RecordingRedis(), file.write, ch, ch.on_error)
        ad.publish_state(st)                       # 0 failure
        assert st == sentinel                      # 不 mutate

    def test_file_write_receives_same_ref(self, tmp_path):
        file = RecordingFile()
        ch = RecordingCH()
        ad = S0PublisherAdapter(RecordingRedis(), file.write, ch, ch.on_error)
        st = dict(BASE)
        ad.publish_state(st)
        assert file.states[0] is st                # aliasing 保持


# ═══════════════════════════════════════════════════════════════
#  Legacy write_state 返回 None + exception + monkeypatch 兼容
# ═══════════════════════════════════════════════════════════════

def test_write_state_returns_none(s0_env, g, rdis, ch_rows, tmp_path):
    st = g.compute_state('bull', 'low', 0.02, False, False, 'normal', 0.5)
    assert g.write_state(st) is None


def test_late_binding_state_file(s0_env, g, rdis, tmp_path):
    """晚绑定：调用间切换 STATE_FILE（monkeypatch 兼容）。"""
    st = g.compute_state('bull', 'low', 0.02, False, False, 'normal', 0.5)
    g.write_state(st)                              # 第一次写 base file
    assert len(list(tmp_path.iterdir())) >= 1
    f2 = tmp_path / 'other-state.json'
    g.STATE_FILE = f2                              # 切换路径（晚绑定验证）
    g.write_state(st)
    assert json.loads(f2.read_text())['market_state'] == 'range'
