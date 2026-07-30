"""
s0_market_guard.py — 宏观市场状态机 v1.1.0
采样: 30s, 写盘: 60s
输出: /root/.openclaw/trade/trading_engine/services/s0/market_state.json
v1.1: 增加统一regime（S6/S7/S8共用）、山寨联动、冲击分
"""
import os, time, json, logging, hmac, hashlib, requests
from urllib.parse import urlencode
from dotenv import load_dotenv
from pathlib import Path
import sys
_BASE = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_BASE / 'services/s0'))
sys.path.insert(0, str(_BASE))
from shared.redis_store import get as _rget, set as _rset
from shared.binance_api import FAPI

load_dotenv(_BASE / 'config/binance.env')
API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
BASE       = FAPI
STATE_FILE = _BASE / 'services/s0/market_state.json'
VERSION    = "1.1.0"

# 日志：stdout + 日期分割文件（logs/s0/YYYYMMDD.log）
LOG_DIR = _BASE.parent / 'logs/s0'
_LOG_FILE = LOG_DIR / f'{time.strftime("%Y%m%d")}.log'
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("s0")
_log_handler = logging.FileHandler(str(_LOG_FILE), encoding='utf-8')
_log_handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
log.addHandler(_log_handler)

BREADTH_N = 50
_breadth_symbols_cache: list = []
_breadth_symbols_ts: float = 0.0

EXCLUDE = {"BTCUSDT", "USDCUSDT", "BUSDUSDT", "TUSDUSDT", "USDTUSDT", "FDUSDUSDT"}

def get_breadth_symbols() -> list:
    global _breadth_symbols_cache, _breadth_symbols_ts
    if _breadth_symbols_cache and time.time() - _breadth_symbols_ts < 6 * 3600:
        return _breadth_symbols_cache
    try:
        tickers = fapi_get("/fapi/v1/ticker/24hr")
        ranked = sorted(
            [t for t in tickers if t["symbol"].endswith("USDT") and t["symbol"] not in EXCLUDE],
            key=lambda t: float(t["quoteVolume"]), reverse=True
        )
        _breadth_symbols_cache = [t["symbol"] for t in ranked[:BREADTH_N]]
        _breadth_symbols_ts = time.time()
        log.info(f"[s0] 宽度采样池已更新: top{BREADTH_N} by volume, e.g. {_breadth_symbols_cache[:5]}")
    except Exception as e:
        log.warning(f"[s0] 获取动态采样池失败，沿用旧列表: {e}")
    return _breadth_symbols_cache


def fapi_get(path, params=None):
    r = requests.get(BASE + path, params=params or {}, timeout=10)
    r.raise_for_status()
    return r.json()


def _s3_window(symbol, tf):
    """从 s3 的 market:s3_data 读取指定币种时间窗"""
    try:
        data = _rget('market:s3_data')
        if data and 'symbols' in data:
            sym_data = data['symbols'].get(symbol, {})
            return sym_data.get(tf, {})
    except Exception:
        pass
    return {}


def _s3_win(w, key, default=0):
    """安全读取窗口字段（兼容 list 和 dict 格式）"""
    if isinstance(w, dict):
        return w.get(key, default)
    return default


def sample_btc():
    w4h  = _s3_window('BTCUSDT', '4h')
    w15m = _s3_window('BTCUSDT', '15m')
    w24h = _s3_window('BTCUSDT', '24h')

    ema20 = _s3_win(w4h, 'ema20')
    ema60 = _s3_win(w4h, 'ema60')
    price = _s3_win(w4h, 'close')

    # ATR 扩张：15m 波动率 > 24h 波动率 × 1.3
    vol_15m = _s3_win(w15m, 'volatility')
    vol_24h = _s3_win(w24h, 'volatility')
    atr_expanding = vol_15m > vol_24h * 1.3 if vol_24h > 0 else False

    # 15m 振幅
    high_15m = _s3_win(w15m, 'high')
    low_15m  = _s3_win(w15m, 'low')
    amp      = (high_15m - low_15m) / price if price > 0 else 0

    if ema20 > ema60 and price > ema20:
        btc_trend = "bull"
    elif ema20 < ema60 and price < ema20:
        btc_trend = "bear"
    else:
        btc_trend = "neutral"

    volatility = "low" if amp < 0.015 else ("normal" if amp < 0.03 else "high")
    btc_below_ema60 = price < ema60

    return btc_trend, volatility, amp, btc_below_ema60, atr_expanding


def sample_breadth():
    above, total = 0, 0
    try:
        data = _rget('market:s3_data')
        symbols_data = (data or {}).get('symbols', {})
    except Exception:
        symbols_data = {}
    scan_list = get_breadth_symbols()
    for sym in scan_list:
        w1h = symbols_data.get(sym, {}).get('1h', {}) if sym in symbols_data else _s3_window(sym, '1h')
        close = _s3_win(w1h, 'close')
        ema20 = _s3_win(w1h, 'ema20')
        if close > ema20 > 0:
            above += 1
        total += 1
    ratio   = above / total if total else 0.5
    breadth = "strong" if ratio > 0.7 else ("normal" if ratio > 0.4 else "weak")
    return breadth, ratio


def sample_alts_sync() -> tuple:
    alts = ['ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT',
            'DOGEUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT', 'MATICUSDT']
    try:
        data = _rget('market:s3_data')
        symbols_data = (data or {}).get('symbols', {})
    except Exception:
        return 0.5, 0, 0

    btc_1h = symbols_data.get('BTCUSDT', {}).get('1h', {}) if 'BTCUSDT' in symbols_data else _s3_window('BTCUSDT', '1h')
    btc_chg = _s3_win(btc_1h, 'chg')
    if btc_chg == 0:
        return 0.5, 0, 0
    btc_direction = 1 if btc_chg > 0 else -1

    following = 0
    total = 0
    for sym in alts:
        w1h = symbols_data.get(sym, {}).get('1h', {}) if sym in symbols_data else _s3_window(sym, '1h')
        chg = _s3_win(w1h, 'chg')
        if chg == 0:
            continue
        if btc_direction > 0:
            following += 1 if chg > -0.5 else 0
        else:
            following += 1 if chg < 0.5 else 0
        total += 1
    sync = following / max(total, 1)
    return round(sync, 2), following, total


def sample_shock_score() -> int:
    """
    冲击分：异常事件的综合检测
    - 价格跳变检测
    - 全市场成交量异常
    - 返回0-10, >5=高冲击
    """
    score = 0
    try:
        tickers = fapi_get("/fapi/v1/ticker/24hr")
        # 统计异常涨幅/跌幅币种数量
        extreme = sum(1 for t in tickers
                      if abs(float(t.get('priceChangePercent', 0))) > 15)
        score += min(extreme // 2, 4)  # 极端波动币越多冲击越大
        # 全市场成交量变化
        if extreme >= 5:
            score += 2
    except Exception:
        pass
    return min(score, 10)


def compute_state(btc_trend, volatility, amp, btc_below_ema60, atr_expanding,
                  breadth, breadth_ratio):
    risk_off = (
        (btc_below_ema60 and atr_expanding) or
        amp > 0.04 or
        breadth_ratio < 0.30
    )
    if risk_off:
        market_state = "risk-off"
    elif btc_trend == "bull" and breadth == "strong":
        market_state = "trend"
    else:
        market_state = "range"

    # ── 统一regime（S7 5档 + S6趋势强度） ──────────────────────────────
    regime = "range"
    regime_score = 0
    trend_strength = 50

    if risk_off:
        regime = "risk-off"
        regime_score = -7
        trend_strength = 0
    elif btc_trend == "bull" and breadth == "strong":
        regime = "bull_trend"
        regime_score = 5
        trend_strength = 85
    elif btc_trend == "bull" and breadth != "weak":
        regime = "weak_bull"
        regime_score = 3
        trend_strength = 65
    elif btc_trend == "bear" or breadth_ratio < 0.35:
        regime = "weak_bear"
        regime_score = -3
        trend_strength = 25
    else:
        regime = "range"
        regime_score = 0
        trend_strength = 50

    # ── 各系统运行许可 ──────────────────────────────────────────────────
    s6_allowed = not risk_off or btc_trend == 'bull'
    s7_allowed = regime in ('range', 'weak_bull')  # 网格只在震荡和弱多运行
    s8_allowed = not risk_off and regime != 'bull_trend'  # 空头不能在强多头开

    new_state = {
        "version":       VERSION,
        "timestamp":     int(time.time()),
        "market_state":  market_state,
        "btc_trend":     btc_trend,
        "breadth":       breadth,
        "breadth_ratio": round(breadth_ratio, 3),
        "volatility":    volatility,
        "risk_off":      risk_off,
        # v1.1 新增字段
        "regime":        regime,
        "regime_score":  regime_score,
        "trend_strength": trend_strength,
        "s6_allowed":    s6_allowed,
        "s7_allowed":    s7_allowed,
        "s8_allowed":    s8_allowed,
    }

    # 采样山寨联动（每30s太频繁，每30分钟采样一次）
    if int(time.time()) % 1800 < 30:
        sync, following, total = sample_alts_sync()
        new_state["alts_sync"] = sync
    else:
        new_state["alts_sync"] = 0.0

    # 冲击分（每60秒更新）
    if int(time.time()) % 60 < 30:
        new_state["shock_score"] = sample_shock_score()
    else:
        new_state["shock_score"] = 0

    return new_state


def write_state(state):
    try:
        _rset('market:s0', state)
    except Exception:
        pass
    tmp = str(STATE_FILE) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, str(STATE_FILE))
    # 同步写入 ClickHouse
    try:
        from shared.clickhouse_client import insert as _ch_insert
        row = json.dumps({
            'market_state':  state['market_state'],
            'btc_trend':     state['btc_trend'],
            'breadth':       state['breadth'],
            'breadth_ratio': state['breadth_ratio'],
            'volatility':    state['volatility'],
            'risk_off':      1 if state['risk_off'] else 0,
        })
        _ch_insert('default.market_state_log', row)
    except Exception as e:
        log.warning(f"CH write error: {e}")


def main():
    last_write = 0
    log.info("s0_market_guard started")
    while True:
        try:
            btc_trend, volatility, amp, btc_below_ema60, atr_expanding = sample_btc()
            breadth, breadth_ratio = sample_breadth()
            state = compute_state(btc_trend, volatility, amp, btc_below_ema60, atr_expanding, breadth, breadth_ratio)

            now = time.time()
            if now - last_write >= 60:
                write_state(state)
                last_write = now
                log.info(f"state={state['market_state']} btc={btc_trend} breadth={breadth}({breadth_ratio:.0%}) vol={volatility} risk_off={state['risk_off']}")

        except Exception as e:
            log.error(f"sample error: {e}")

        time.sleep(30)


if __name__ == "__main__":
    main()
