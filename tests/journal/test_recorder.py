"""Journal Recorder 测试：Null / Memory / fail-open。"""

from journal import DecisionJournalBuilder
from journal.recorder import (
    MemoryJournalRecorder,
    NullJournalRecorder,
    get_default_recorder,
    safe_record,
    set_default_recorder,
)


class BrokenRecorder:
    """record 必抛的 Recorder（fail-open 测试用）。"""

    def record(self, journal):
        raise RuntimeError('boom')


def _builder():
    return DecisionJournalBuilder(signal_source='S3', signal_type='TREND_UP',
                                  symbol='BTCUSDT', side='LONG')


def test_null_recorder_no_op():
    """NullRecorder.record 不抛异常、不返回内容。"""
    r = NullJournalRecorder()
    assert r.record(_builder().build(action='OPEN', accepted=True)) is None


def test_default_recorder_is_null():
    """进程默认 Recorder 是 Null（生产零 IO）。"""
    assert isinstance(get_default_recorder(), NullJournalRecorder)


def test_set_default_recorder_switch_and_restore(monkeypatch):
    """set_default_recorder 可切换；monkeypatch 自动还原，不污染其他测试。"""
    mem = MemoryJournalRecorder()
    monkeypatch.setattr('journal.recorder._default_recorder', mem)
    assert get_default_recorder() is mem
    set_default_recorder(None)  # None → 回退 Null
    assert isinstance(get_default_recorder(), NullJournalRecorder)


def test_memory_recorder_captures():
    """MemoryRecorder 能捕获 Journal，records[0] 即原对象。"""
    mem = MemoryJournalRecorder()
    jb = _builder()
    j = jb.build(action='OPEN', accepted=True)
    mem.record(j)
    assert len(mem) == 1
    assert mem.records[0] is j


def test_memory_recorder_bounded():
    """MemoryRecorder 有界：超 maxlen 后丢弃最旧。"""
    mem = MemoryJournalRecorder(maxlen=2)
    for _ in range(3):
        mem.record(_builder().build(action='OPEN', accepted=True))
    assert len(mem) == 2


def test_safe_record_happy_path():
    """safe_record：组装 + 记录成功。"""
    mem = MemoryJournalRecorder()
    safe_record(mem, _builder(), action='REJECT', accepted=False, reason='x')
    assert len(mem) == 1
    assert mem.records[0].decision.action == 'REJECT'
    assert mem.records[0].decision.reason == 'x'


def test_safe_record_fail_open_broken_recorder():
    """Recorder 抛异常 → safe_record 吞掉并经 on_error 上报，绝不向外抛。"""
    errors = []
    safe_record(BrokenRecorder(), _builder(), action='OPEN', accepted=True,
                on_error=errors.append)
    assert len(errors) == 1
    assert 'fail-open' in errors[0]
    assert 'boom' in errors[0]


def test_safe_record_fail_open_broken_builder(monkeypatch):
    """builder.build 抛异常（白盒注入）→ 不抛、不记录。"""
    jb = _builder()

    def _boom(**kw):
        raise RuntimeError('build boom')

    monkeypatch.setattr(jb, 'build', _boom)
    mem = MemoryJournalRecorder()
    errors = []
    safe_record(mem, jb, action='OPEN', accepted=True, on_error=errors.append)
    assert len(mem) == 0
    assert len(errors) == 1


def test_safe_record_on_error_itself_raises():
    """on_error 自身再抛 → 二次吞掉，仍不向外抛。"""
    def _bad_on_error(msg):
        raise RuntimeError('log failed')

    safe_record(BrokenRecorder(), _builder(), action='OPEN', accepted=True,
                on_error=_bad_on_error)  # 不应抛


def test_safe_record_skips_none_journal():
    """builder.build 返回 None（内部损坏）→ 不记录、不报错。"""
    jb = _builder()
    jb._broken = True  # 白盒
    mem = MemoryJournalRecorder()
    safe_record(mem, jb, action='OPEN', accepted=True)
    assert len(mem) == 0
