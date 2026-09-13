"""S0 characterization fixtures。

隔离：无真实 Binance/Redis/CH/文件写/sleep；redis_store 与 fapi_get/requests
以 spy 注入；时钟由 monkeypatch 冻结。
"""
import json
import time

import pytest

BASE_TS = 1_700_000_000


class FakeClock:
    def __init__(self, start: float = BASE_TS):
        self.now = start

    def time(self) -> float:
        return self.now

    def advance(self, d: float):
        self.now += d


class SqliteSpy(list):  # noqa placeholder name, truly a list-type helper
    pass


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.writes = []
        self.reads = []
        self.set_exc = None
        self.get_exc = None

    def get(self, key):
        self.reads.append(key)
        if self.get_exc is not None:
            raise self.get_exc
        return self.store.get(key)

    def set(self, key, val):
        self.writes.append((key, val))
        if self.set_exc is not None:
            raise self.set_exc
        self.store[key] = val


@pytest.fixture
def s0_env(monkeypatch, tmp_path):
    """S0 模块隔离 + fake clock/redis + CH spy + 文件重定向。"""
    import services.s0.s0_market_guard as g
    saved = {k: getattr(g, k, None) for k in
             ('_breadth_symbols_cache', '_breadth_symbols_ts')}
    monkeypatch.setattr(g, '_breadth_symbols_cache', [])
    monkeypatch.setattr(g, '_breadth_symbols_ts', 0.0)

    fr = FakeRedis()
    fake = FakeClock(BASE_TS)
    monkeypatch.setattr(g, '_rget', fr.get)
    monkeypatch.setattr(g, '_rset', fr.set)
    monkeypatch.setattr(g, 'time', fake)
    monkeypatch.setattr(g, 'STATE_FILE', tmp_path / 'market_state.json')

    ch_rows = []

    def ch_insert(table, row):
        ch_rows.append((table, row))
        return True
    monkeypatch.setattr('shared.clickhouse_client.insert', ch_insert)
    return g, fr, fake, ch_rows, tmp_path


@pytest.fixture
def g(s0_env):
    (g, fr, fake, ch_rows) = (None, None, None, None)
    (g, fr, fake, ch, r) = s0_env
    return g


@pytest.fixture
def rdis(s0_env):
    (_g, fr, _fk, _ch, _r) = s0_env
    return fr


@pytest.fixture
def clock(s0_env):
    (_g, _fr, fk, _ch, _r) = s0_env
    return fk


@pytest.fixture
def ch_rows(s0_env):
    (_g, _fr, _fk, ch, _r) = s0_env
    return ch


# ── 数据生成 helpers ─────────────────────────────────────────────────

def sym_row(sym, close, ema20=100.0, ema60=95.0, chg=1.0, volatility=0.0,
            high=None, low=None):
    return {'close': close, 'ema20': ema20, 'ema60': ema60, 'chg': chg,
            'volatility': volatility,
            'high': high if high is not None else close * 1.01,
            'low': low if low is not None else close * 0.99}


def seed_s3(state_store, rows):
    """market:s3_data {symbols: {sym: {1h/4h/15m/24h}}}"""
    state_store['market:s3_data'] = {'ts': BASE_TS, 'symbols': rows}
