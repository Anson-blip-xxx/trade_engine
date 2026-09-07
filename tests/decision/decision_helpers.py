"""Decision characterization 共享夹具与补丁助手（P3-01）。

隔离原则：只 patch IO / 进程状态（Binance ticker、S0 读取、PM 缓存、
冷却内存、余额 API、analysis 存储）；被测的 Decision 业务函数
（contract_score / classify_entry_mode / price_is_overextended /
bounded_stop_pct / leverage_for_score / long_trend_takeover_ready /
pump_down_uptrend_guard / long/short_signal_allows_open）全部走真实实现。
"""
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / 'strategies')):
    if _p not in sys.path:
        sys.path.insert(0, _p)



EVENT_LONG = {'symbol': 'TESTUSDT', 'type': 'TREND_UP', 'strength': 70}
EVENT_SHORT = {'symbol': 'TESTUSDT', 'type': 'TREND_DOWN', 'strength': 70}
EVENT_VIOLANT_BULL = {'symbol': 'TESTUSDT', 'type': 'VIOLENT_BULLISH', 'strength': 88}
EVENT_PUMP_UP = {'symbol': 'TESTUSDT', 'type': 'PUMP_UP', 'strength': 70}
EVENT_PUMP_DOWN = {'symbol': 'TESTUSDT', 'type': 'PUMP_DOWN', 'strength': 70}

MARKET_LONG = {'TESTUSDT': {'1h': {'ema20': 99.0, 'atr': 1.0, 'atr_pct': 2.0},
                            '15m': {'rsi': 55.0},
                            '4h': {'chg': 1.0}, '24h': {'chg': 5.0}}}
MARKET_SHORT = {'TESTUSDT': {'1h': {'ema20': 101.0, 'atr': 1.0, 'atr_pct': 2.0},
                             '15m': {'rsi': 45.0},
                             '4h': {'chg': -1.0}, '24h': {'chg': -5.0}}}
# S6B takeover 市场形态（price=100 > ema24=98 > ema60=90, chg4h>0, flow≥0.52, 距离近）
MARKET_TAKEOVER = {'TESTUSDT': {'1h': {'ema20': 99.0, 'atr': 1.0, 'atr_pct': 2.0},
                                '15m': {'rsi': 60.0, 'ema20': 99.5, 'atr': 0.5,
                                        'taker_buy_ratio': 0.60},
                                '4h': {'ema20': 98.0, 'chg': 5.0},
                                '24h': {'ema20': 95.0, 'ema60': 90.0, 'chg': 15.0}}}

S6_GATE_ORDER = ['regime', 'fresh', 'signal_cooldown', 'position_limit',
                 'symbol_cooldown', 'market_allowed', 'existing_position',
                 'price', 'entry_mode', 'trend', 'extension', 'atr']
S8_GATE_ORDER = ['strength', 'fresh', 'signal_cooldown', 'position_limit',
                 'symbol_cooldown', 'market_allowed', 'existing_position',
                 'price', 'entry_mode', 'trend', 'extension', 'atr']


def patch_s6(monkeypatch, mod, *, recorder=None, stale=False, sig_fresh=True,
             dd_mode='normal', pos_count=0, market_allows=True, has_pos=False,
             price='100.0', replace_ok=True, calc_qty=0.5, short_ratio=0.5):
    calls = {'open': [], 'release': []}

    def _open(*args, **kwargs):
        calls['open'].append({'args': args, 'kwargs': kwargs})
        return True
    monkeypatch.setattr(mod, 'open_position', _open)
    monkeypatch.setattr(mod, 'get_market_state',
                        lambda: {'regime': 'range', 'timestamp': 1788000000})
    monkeypatch.setattr(mod, 'event_is_stale', lambda evt: stale)
    monkeypatch.setattr(mod, 'is_event_fresh', lambda *a, **k: sig_fresh)
    monkeypatch.setattr(mod, 'drawdown_mode', lambda: dd_mode)
    monkeypatch.setattr(mod, 'maybe_replace_recovery_position',
                        lambda *a, **k: replace_ok)
    monkeypatch.setattr(mod, 'get_position_count', lambda name: pos_count)
    monkeypatch.setattr(mod, 'market_allows_trading', lambda name, side: market_allows)
    monkeypatch.setattr(mod, 'has_any_position', lambda sym: has_pos)
    monkeypatch.setattr(mod, 'fapi_get', lambda path, params=None: {'price': price})
    monkeypatch.setattr(mod, 'get_short_ratio', lambda sym: short_ratio)
    monkeypatch.setattr(mod, 'calc_position_qty', lambda *a, **k: calc_qty)
    monkeypatch.setattr(mod, 'release_event_fresh', lambda *a, **k: calls['release'].append(1))
    if recorder is not None:
        monkeypatch.setattr(mod, 'get_default_recorder', lambda: recorder)
    return calls


def patch_s8(monkeypatch, mod, *, recorder=None, stale=False, sig_fresh=True,
             dd_mode='normal', pos_count=0, market_allows=True, has_pos=False,
             price='100.0', replace_ok=True, calc_qty=0.5, short_ratio=0.5,
             market=None):
    calls = {'open': [], 'release': []}

    def _open(*args, **kwargs):
        calls['open'].append({'args': args, 'kwargs': kwargs})
        return True
    monkeypatch.setattr(mod, 'open_position', _open)
    monkeypatch.setattr(mod, 'event_is_stale', lambda evt: stale)
    monkeypatch.setattr(mod, 'is_event_fresh', lambda *a, **k: sig_fresh)
    monkeypatch.setattr(mod, 'drawdown_mode', lambda: dd_mode)
    monkeypatch.setattr(mod, 'maybe_replace_recovery_position',
                        lambda *a, **k: replace_ok)
    monkeypatch.setattr(mod, 'get_position_count', lambda name: pos_count)
    monkeypatch.setattr(mod, 'market_allows_trading', lambda name, side: market_allows)
    monkeypatch.setattr(mod, 'has_any_position', lambda sym: has_pos)
    monkeypatch.setattr(mod, 'fapi_get', lambda path, params=None: {'price': price})
    monkeypatch.setattr(mod, 'get_short_ratio', lambda sym: short_ratio)
    monkeypatch.setattr(mod, 'calc_position_qty', lambda *a, **k: calc_qty)
    monkeypatch.setattr(mod, 'release_event_fresh', lambda *a, **k: calls['release'].append(1))
    if market is not None:
        monkeypatch.setattr(mod, 'read_s3_market_data', lambda: market)
    if recorder is not None:
        monkeypatch.setattr(mod, 'get_default_recorder', lambda: recorder)
    return calls


def run_s6(mod, evt, market=MARKET_LONG, state=None):
    return mod._open_long(dict(state or {'cooldowns': {}}), dict(evt), market)


def run_s8(mod, evt, market=MARKET_SHORT, state=None):
    return mod._open_short(dict(state or {'cooldowns': {}}), dict(evt), market)


def gate_names(journal):
    return [g.name for g in journal.gates]


def decision_of(journal):
    return journal.decision


__all__ = [
    'EVENT_LONG',
    'EVENT_PUMP_DOWN',
    'EVENT_PUMP_UP',
    'EVENT_SHORT',
    'EVENT_VIOLANT_BULL',
    'MARKET_LONG',
    'MARKET_SHORT',
    'MARKET_TAKEOVER',
    'S6_GATE_ORDER',
    'S8_GATE_ORDER',
    'decision_of',
    'gate_names',
    'patch_s6',
    'patch_s8',
    'run_s6',
    'run_s8',
    'time',
]
