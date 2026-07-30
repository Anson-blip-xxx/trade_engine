"""
市场数据 — 价格、K线、指标、评分等
依赖: binance_api (FAPI, API_KEY), indicators, data_cache
"""
import time, requests
import sys as _sys
from datetime import datetime
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_sys.path.insert(0, str(_BASE))
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from shared.binance_api import FAPI, fapi_get
from indicators import calc_ema, calc_rsi, calc_macd_hist

# ── 通用缓存装饰器 ──
_cache = {}
def cached(ttl):
    def deco(fn):
        def wrapper(*args, **kwargs):
            key = f"{fn.__name__}:{args}:{sorted(kwargs.items())}"
            now = time.time()
            if key in _cache and _cache[key][1] > now:
                return _cache[key][0]
            val = fn(*args, **kwargs)
            if val is not None:
                _cache[key] = (val, now + ttl)
            return val
        return wrapper
    return deco

# ── 价格缓存 ──
_price_cache: dict = {}
_price_cache_ts: float = 0
_PRICE_CACHE_TTL = 5

def _refresh_price_cache():
    global _price_cache, _price_cache_ts
    if time.time() - _price_cache_ts < _PRICE_CACHE_TTL:
        return
    try:
        r = requests.get(f'{FAPI}/fapi/v1/ticker/price', timeout=5)
        data = r.json()
        if isinstance(data, list):
            _price_cache = {d['symbol']: float(d['price']) for d in data}
            _price_cache_ts = time.time()
    except Exception:
        pass

def batch_get_prices(symbols: list) -> dict:
    if not symbols:
        return {}
    _refresh_price_cache()
    return {s: _price_cache[s] for s in symbols if s in _price_cache}

def get_price(symbol):
    try:
        from shared.shared_positions import get_live_price
        p = get_live_price(symbol)
        if p > 0:
            return p
    except Exception:
        pass
    _refresh_price_cache()
    if symbol in _price_cache:
        return _price_cache[symbol]
    r = requests.get(f'{FAPI}/fapi/v1/ticker/price', params={'symbol': symbol}, timeout=5)
    data = r.json()
    if 'price' not in data:
        raise ValueError(f'get_price failed: {data}')
    return float(data['price'])


# ── 余额 ──
@cached(20)
def get_balance():
    data = fapi_get('/fapi/v2/balance')
    for item in data:
        if item['asset'] == 'USDT':
            return float(item['balance']) + float(item['crossUnPnl'])
    return 0


# ── K线 ──
_klines_cache: dict = {}
_KLINES_TTL = 300

def get_klines(symbol, interval='1h', limit=20):
    key = (symbol, interval, limit)
    cached_entry = _klines_cache.get(key)
    if cached_entry and time.time() - cached_entry[1] < _KLINES_TTL:
        return cached_entry[0]
    r = requests.get(f'{FAPI}/fapi/v1/klines',
        params={'symbol': symbol, 'interval': interval, 'limit': limit}, timeout=10)
    data = r.json()
    result = data if isinstance(data, list) else []
    if result:
        _klines_cache[key] = (result, time.time())
    return result


# ── 技术指标 ──
def get_ma(symbol, period=20):
    klines = get_klines(symbol, '1d', period + 1)
    closes = [float(k[4]) for k in klines]
    return sum(closes[-period:]) / period if len(closes) >= period else 0

def get_ema(symbol, period=20, interval='1h'):
    klines = get_klines(symbol, interval, period * 2)
    closes = [float(k[4]) for k in klines]
    return calc_ema(closes, period)

def get_rsi(symbol, period=14, interval='1h'):
    klines = get_klines(symbol, interval, period + 2)
    closes = [float(k[4]) for k in klines]
    return calc_rsi(closes, period)

def get_macd(symbol, interval='1h'):
    klines = get_klines(symbol, interval, 60)
    closes = [float(k[4]) for k in klines]
    return calc_macd_hist(closes)


# ── ATR ──
def get_atr(symbol, period=14):
    r = requests.get(f'{FAPI}/fapi/v1/klines',
        params={'symbol': symbol, 'interval': '1h', 'limit': period + 1}, timeout=10)
    klines = r.json()
    if not isinstance(klines, list):
        return 0
    trs = []
    for i in range(1, len(klines)):
        high = float(klines[i][2])
        low = float(klines[i][3])
        prev_close = float(klines[i-1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    return sum(trs) / len(trs) if trs else 0

def is_atr_compressed(symbol):
    try:
        klines = get_klines(symbol, '1h', 45)
        if len(klines) < 45:
            return False, 1.0
        atrs = []
        for i in range(15, len(klines)):
            trs = []
            for j in range(max(0, i-14), i):
                high = float(klines[j][2])
                low = float(klines[j][3])
                prev_close = float(klines[j-1][4]) if j > 0 else float(klines[j][1])
                tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
                trs.append(tr)
            atrs.append(sum(trs) / len(trs) if trs else 0)
        current_atr = atrs[-1]
        avg_atr = sum(atrs[-30:]) / 30
        if avg_atr == 0:
            return False, 1.0
        ratio = current_atr / avg_atr
        return ratio < 0.7, ratio
    except Exception:
        return False, 1.0


# ── 合约信息 ──
def get_symbol_info(symbol):
    r = requests.get(f'{FAPI}/fapi/v1/exchangeInfo', timeout=10)
    for s in r.json()['symbols']:
        if s['symbol'] == symbol:
            return s['quantityPrecision'], s['pricePrecision']
    return 3, 2


# ── OI / 资金费率 ──
@cached(480)
def get_oi_and_funding(symbol):
    try:
        fr = requests.get(f'{FAPI}/fapi/v1/premiumIndex', params={'symbol': symbol}, timeout=5).json()
        funding = float(fr.get('lastFundingRate', 0))
        oi_hist = requests.get(f'{FAPI}/futures/data/openInterestHist',
            params={'symbol': symbol, 'period': '30m', 'limit': 2}, timeout=5).json()
        if len(oi_hist) >= 2:
            oi_chg = (float(oi_hist[-1]['sumOpenInterest']) - float(oi_hist[-2]['sumOpenInterest'])) / float(oi_hist[-2]['sumOpenInterest']) * 100
        else:
            oi_chg = 0
        klines = get_klines(symbol, '30m', 2)
        if len(klines) >= 2:
            price_chg = (float(klines[-1][4]) - float(klines[-2][4])) / float(klines[-2][4]) * 100
        else:
            price_chg = 0
        return funding, oi_chg, price_chg
    except Exception:
        return 0, 0, 0

@cached(55)
def get_current_oi(symbol) -> float:
    try:
        r = requests.get(f'{FAPI}/futures/data/openInterestHist',
            params={'symbol': symbol, 'period': '30m', 'limit': 1}, timeout=5).json()
        return float(r[-1]['sumOpenInterest']) if r else 0.0
    except Exception:
        return 0.0


# ── 杠杆计算 ──
MAX_LEVERAGE = 10

def calc_leverage(atr, price):
    vol_pct = atr / price * 100
    if vol_pct < 1:   lev = 10
    elif vol_pct < 2: lev = 7
    elif vol_pct < 3: lev = 5
    else:             lev = 3
    return min(lev, MAX_LEVERAGE)


# ── Top Symbols ──
_TOP_SYMBOLS_CACHE: list = []
_TOP_SYMBOLS_TS: float = 0
_EXCLUDE_STABLE = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "USDTUSDT", "FDUSDUSDT"}

def get_top_symbols(n=50) -> list:
    global _TOP_SYMBOLS_CACHE, _TOP_SYMBOLS_TS
    if _TOP_SYMBOLS_CACHE and time.time() - _TOP_SYMBOLS_TS < 6 * 3600:
        return _TOP_SYMBOLS_CACHE
    try:
        tickers = requests.get(f'{FAPI}/fapi/v1/ticker/24hr', timeout=10).json()
        ranked = sorted(
            [t for t in tickers if t['symbol'].endswith('USDT') and t['symbol'] not in _EXCLUDE_STABLE],
            key=lambda t: float(t['quoteVolume']), reverse=True
        )[:n]
        _TOP_SYMBOLS_CACHE = [t['symbol'] for t in ranked]
        _TOP_SYMBOLS_TS = time.time()
    except Exception:
        pass
    return _TOP_SYMBOLS_CACHE


# ── TradFi 黑名单 ──
@cached(86400)
def _fetch_tradfi_blacklist():
    blacklist = set()
    try:
        r = requests.get(f'{FAPI}/fapi/v1/exchangeInfo', timeout=10)
        for s in r.json().get('symbols', []):
            if s.get('underlyingType') != 'COIN' and s.get('contractType') == 'PERPETUAL':
                blacklist.add(s['symbol'])
    except Exception:
        pass
    return blacklist

def is_tradfi(symbol):
    return symbol.replace('_SHORT', '') in _fetch_tradfi_blacklist()


# ── 信号评分 ──
@cached(55)
def score_signal(symbol, funding_rate=None, oi_chg_pct=None, signal_source=''):
    score = 0
    try:
        price = get_price(symbol)
        atr = get_atr(symbol)
        ma20 = get_ma(symbol)
        support = get_support_level(symbol)
        sl_pct = (price - support * 0.995) / price * 100

        if funding_rate is None or oi_chg_pct is None:
            try:
                _f, _oi, _ = get_oi_and_funding(symbol)
                if funding_rate is None:  funding_rate = _f
                if oi_chg_pct is None:    oi_chg_pct = _oi
            except Exception:
                funding_rate = funding_rate or 0
                oi_chg_pct   = oi_chg_pct or 0

        if funding_rate < -0.05:   score += 2
        elif funding_rate < -0.02: score += 1

        if oi_chg_pct > 20:   score += 2
        elif oi_chg_pct > 10: score += 1

        if ma20 > 0:
            dist = (price - ma20) / ma20 * 100
            if 0 < dist < 1:   score += 2
            elif 0 < dist < 3: score += 1

        vol_pct = atr / price * 100 if price > 0 else 0
        if 1 <= vol_pct <= 2: score += 2
        elif vol_pct < 3:     score += 1

        if sl_pct < 1:   score += 2
        elif sl_pct < 2: score += 1

        try:
            from shared.redis_store import get as _rget
            _s0 = _rget('market:s0')
            if _s0:
                if _s0.get('breadth') == 'strong':   score += 2
                elif _s0.get('breadth') == 'normal': score += 1
                if _s0.get('btc_trend') == 'bull':   score += 1
        except Exception:
            pass

        if signal_source in ('s2a', 's2e', 's2j'):  score += 1
    except Exception:
        pass
    return score


# ── 市场状态 ──
def is_strict_hour():
    hour = datetime.now().hour
    return 4 <= hour < 20

def is_high_risk_hour():
    hour = datetime.now().hour
    minute = datetime.now().minute
    return (hour == 21 and minute >= 30) or (22 <= hour <= 23) or (0 <= hour < 2)

def get_market_state():
    try:
        from shared.redis_store import get as _rget
        _s0 = _rget('market:s0')
        if _s0 and 'market_state' in _s0:
            _ms = _s0['market_state']
            _score = _s0.get('trend_strength', 50)
            return _ms, _score
    except Exception:
        pass
    return 'range', 50

def get_market_breadth():
    try:
        tickers = requests.get(f'{FAPI}/fapi/v1/ticker/24hr', timeout=10).json()
        top_symbols = sorted([t for t in tickers if t['symbol'].endswith('USDT')],
                            key=lambda x: float(x['quoteVolume']), reverse=True)[:100]
        above_ema20 = 0
        total_checked = 0
        for ticker in top_symbols[:50]:
            try:
                symbol = ticker['symbol']
                price = float(ticker['lastPrice'])
                ema20 = get_ema(symbol, 20, '1d')
                if ema20 > 0:
                    total_checked += 1
                    if price > ema20:
                        above_ema20 += 1
            except Exception:
                continue
        if total_checked == 0:
            return 50, 'normal'
        breadth_pct = above_ema20 / total_checked * 100
        if breadth_pct > 60:
            mode = 'strong'
        elif breadth_pct > 30:
            mode = 'normal'
        else:
            mode = 'weak'
        return breadth_pct, mode
    except Exception:
        return 50, 'normal'


# ── 信号新鲜度 ──
def get_signal_freshness(symbol):
    try:
        klines = get_klines(symbol, '1h', 35)
        if len(klines) < 35:
            return 50
        closes = [float(k[4]) for k in klines]
        ema20_values = []
        ema60_values = []
        for i in range(20, len(closes)):
            ema20 = sum(closes[i-20:i]) / 20
            ema20_values.append(ema20)
        for i in range(60, len(closes)):
            ema60 = sum(closes[i-60:i]) / 60
            ema60_values.append(ema60)
        golden_cross_bars = 0
        for i in range(min(len(ema20_values), len(ema60_values)) - 1, 0, -1):
            if ema20_values[i] > ema60_values[i]:
                golden_cross_bars += 1
            else:
                break
        if golden_cross_bars < 5:
            return 100
        elif golden_cross_bars < 15:
            return 80
        elif golden_cross_bars < 30:
            return 50
        else:
            return 20
    except Exception:
        return 50


# ── BTC波动率 / 支撑 / 区间高点 ──
@cached(60)
def get_btc_volatility():
    try:
        klines = get_klines('BTCUSDT', '15m', 4)
        if len(klines) < 4:
            return 0
        highs = [float(k[2]) for k in klines]
        lows = [float(k[3]) for k in klines]
        return (max(highs) - min(lows)) / min(lows) * 100
    except Exception:
        return 0

@cached(300)
def get_recent_high(symbol, hours=4):
    klines = get_klines(symbol, '1h', hours)
    return max(float(k[2]) for k in klines) if klines else 0

def get_support_level(symbol):
    klines = get_klines(symbol, '1h', 20)
    lows = [float(k[3]) for k in klines]
    return min(lows) if lows else 0


# ── 凯利仓位计算 ──
MAX_POSITION_PCT = 0.10

def calc_position_size(balance, history):
    if len(history) >= 3:
        recent_results = [t['result'] for t in history[-3:]]
        if recent_results.count('loss') >= 3:
            return 0.05
    if len(history) < 5:
        return MAX_POSITION_PCT
    recent = history[-10:]
    wins = [t for t in recent if t['result'] == 'win']
    losses = [t for t in recent if t['result'] == 'loss']
    win_rate = len(wins) / len(recent)
    avg_win = sum(t['pct'] for t in wins) / len(wins) if wins else 1
    avg_loss = abs(sum(t['pct'] for t in losses) / len(losses)) if losses else 1
    kelly = win_rate / avg_loss - (1 - win_rate) / avg_win if avg_win > 0 else 0
    kelly = max(0.03, min(0.15, kelly * 0.5))
    recent_results = [t['result'] for t in history[-3:]]
    if recent_results.count('loss') >= 3:
        kelly = min(kelly, 0.05)
    return kelly
