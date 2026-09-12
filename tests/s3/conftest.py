"""S3 characterization fixtures（P5-01）。

隔离原则：
- 不发真实 HTTP / WS / Redis / 文件日志；时间用受控 fake
- 每个 test 模块级 state 重置（可独立运行）
- IO 只 mock 最外层 helpers（_rset/_rpublish/requests/LOG_DIR）
"""
import pytest

import strategies.s3_orderflow as s3

#: P5-00 inventory 识别的全部模块级可变状态
RESET_KEYS = ('_event_states', '_fb_state', '_ema_cache', '_symbol_klines',
              '_symbol_windows', '_symbol_windows_raw', '_current_kline',
              '_big_orders', '_API_LAST_CALL', '_last_heartbeat')


class FakeTime:
    """受控 time 模块替身：time() 递增推针、sleep 记录不等待。"""

    def __init__(self, start: float = 1_700_000_000.0):
        self.now = start
        self.sleeps = []

    def time(self) -> float:
        return self.now

    def sleep(self, d: float):
        self.sleeps.append(d)
        self.now += d

    def advance(self, d: float):
        self.now += d

    def strftime(self, fmt, t=None):  # _log 用
        import time as _t
        return _t.strftime(fmt, _t.gmtime(self.now))


@pytest.fixture(autouse=True)
def s3_env(monkeypatch, tmp_path):
    """每个 test：模块态清零 + 日志隔离 + fake clock 注入（不真实 sleep）。"""
    import strategies.s3_orderflow as s3
    orig = {k: getattr(s3, k, None) for k in RESET_KEYS}

    for k in RESET_KEYS:
        if isinstance(orig[k], dict):
            monkeypatch.setattr(s3, k, {})
        elif isinstance(orig[k], list):
            monkeypatch.setattr(s3, k, [])
        elif isinstance(orig[k], (int, float)):
            monkeypatch.setattr(s3, k, 0)

    monkeypatch.setattr(s3, '_LOG_DIR', tmp_path / 's3log')
    monkeypatch.setattr(s3, '_ws_kline_connected', False)

    fake_time = FakeTime()
    monkeypatch.setattr(s3, 'time', fake_time)
    yield s3, fake_time
    # monkeypatch 自动还原全部 attrs


@pytest.fixture
def s3m(s3_env):
    (s3, fake_time) = s3_env
    return s3


@pytest.fixture
def clock(s3_env):
    (_s3, fake_time) = s3_env
    return fake_time


@pytest.fixture
def redis_spy(monkeypatch):
    """拦截 s3 的 Redis 输出（_rset/_rpublish/_rget）。"""
    import strategies.s3_orderflow as s3

    class Spy:
        def __init__(self, store=None):
            self.store = {} if store is None else store
            self.writes = []
            self.publishes = []
            self.set_exc = None
            self.publish_exc = None
            self.get_exc = None

        def set(self, key, val):
            self.writes.append({'key': key, 'val': val})
            if self.set_exc is not None:
                raise self.set_exc
            self.store[key] = val

        def publish(self, channel, message='1'):
            self.publishes.append({'channel': channel, 'message': message})
            if self.publish_exc is not None:
                raise self.publish_exc
            return True

        def get(self, key):
            if self.get_exc is not None:
                raise self.get_exc
            return self.store.get(key)

    spy = Spy()
    monkeypatch.setattr(s3, '_rset', spy.set)
    monkeypatch.setattr(s3, '_rpublish', spy.publish)
    monkeypatch.setattr(s3, '_rget', spy.get)
    return spy


# ── 窗口构造 helper（直接构造，不经过 fetch；仅 S3 纯函数路径） ──────────

def w15m(close=100.0, chg=0.0, vol_ratio=1.0, atr_pct=0.1, volatility=0.0,
         high=101.0, low=99.0, rsi=50.0, ema20=100.0, ema60=100.0,
         taker_ratio=None, **extra):
    d = {'close': close, 'chg': chg, 'vol_ratio': vol_ratio,
         'atr_pct': atr_pct, 'volatility': volatility, 'high': high,
         'low': low, 'rsi': rsi, 'ema20': ema20, 'ema60': ema60,
         'volume': 1000.0}
    if taker_ratio is not None:
        d['taker_buy_ratio'] = taker_ratio
    d.update(extra)
    return d


def w1h(chg=0.0, volatility=0.0, high=101.0, low=99.0, ema20=100.0,
        ema60=100.0, atr_pct=0.05, **extra):
    d = {'chg': chg, 'volatility': volatility, 'high': high, 'low': low,
         'ema20': ema20, 'ema60': ema60, 'atr_pct': atr_pct, 'close': 100.0}
    d.update(extra)
    return d


def w24h(chg=0.0, ema20=100.0, ema60=100.0, **extra):
    d = {'chg': chg, 'ema20': ema20, 'ema60': ema60}
    d.update(extra)
    return d


def windows(w15m, h1=None, h4=None, d24=None):
    return {'15m': w15m, '1h': h1 if h1 is not None else w1h(),
            '4h': h4 if h4 is not None else w1h(),
            '24h': d24 if d24 is not None else w24h()}


def detect(s3m, w, raw=None):
    raw = raw or {}
    return s3m.detect_events('TESTUSDT', w, raw)
