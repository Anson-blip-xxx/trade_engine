"""
s0_market_guard.py — 宏观市场状态机 v1.1.0
采样: 30s, 写盘: 60s
输出: /root/.openclaw/trade/trading_engine/services/s0/market_state.json
v1.1: 增加统一regime（S6/S7/S8共用）、山寨联动、冲击分
"""
import os, time, json, logging, hmac, hashlib, requests
from urllib.parse import urlencode
from dotenv import load_dotenv
import sys
sys.path.insert(0, "/root/.openclaw/trade/trading_engine/services/s0")
sys.path.insert(0, "/root/.openclaw/trade/trading_engine")
from indicators import calc_ema, calc_atr
from shared.redis_store import set as _rset

load_dotenv("/root/.openclaw/trade/trading_engine/config/binance.env")
API_KEY    = os.getenv("BINANCE_API_KEY")
API_SECRET = os.getenv("BINANCE_API_SECRET")
BASE       = "https://fapi.binance.com"
STATE_FILE = "/root/.openclaw/trade/trading_engine/services/s0/market_state.json"
VERSION    = "1.1.0"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
log = logging.getLogger("s0")

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


def get_klines(symbol, interval, limit):
    return fapi_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})


def sample_btc():
    k4h = get_klines("BTCUSDT", "4h", 70)
    closes = [float(k[4]) for k in k4h]
    highs  = [float(k[2]) for k in k4h]
    lows   = [float(k[3]) for k in k4h]

    ema20 = calc_ema(closes, 20)
    ema60 = calc_ema(closes, 60)
    price = closes[-1]

    atr_recent = calc_atr(k4h[-4:],  3)
    atr_avg    = calc_atr(k4h[-21:], 20)
    atr_expanding = atr_recent > atr_avg * 1.3

    k15m = get_klines("BTCUSDT", "15m", 3)
    last = k15m[-2]
    amp  = (float(last[2]) - float(last[3])) / float(last[1])

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
    for sym in get_breadth_symbols():
        try:
            k = get_klines(sym, "1h", 22)
            closes = [float(x[4]) for x in k]
            ema20  = calc_ema(closes, 20)
            if closes[-1] > ema20:
                above += 1
            total += 1
        except Exception:
            pass
    ratio   = above / total if total else 0.5
    breadth = "strong" if ratio > 0.7 else ("normal" if ratio > 0.4 else "weak")
    return breadth, ratio


def sample_alts_sync() -> tuple:
    """
    山寨联动度：跟踪主流山寨是否跟随BTC方向
    返回 (sync_ratio, alts_bullish_count, alts_total)
    """
    alts = ['ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT', 'ADAUSDT',
            'DOGEUSDT', 'AVAXUSDT', 'DOTUSDT', 'LINKUSDT', 'MATICUSDT']
    try:
        btc_k = get_klines('BTCUSDT', '1h', 2)
        btc_direction = 1 if float(btc_k[-1][4]) >= float(btc_k[-2][4]) else -1
    except Exception:
        return 0.5, 0, 0

    following = 0
    total = 0
    for sym in alts:
        try:
            k = get_klines(sym, '1h', 2)
            if k and len(k) >= 2:
                alt_pct = (float(k[-1][4]) - float(k[-2][4])) / float(k[-2][4]) * 100
                btc_pct = 0
                if btc_direction > 0:
                    following += 1 if alt_pct > -0.5 else 0  # 跟随或小幅背离都算
                else:
                    following += 1 if alt_pct < 0.5 else 0
                total += 1
        except Exception:
            pass
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
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, STATE_FILE)
    # 同步写入 ClickHouse
    try:
        import subprocess
        row = json.dumps({
            'market_state':  state['market_state'],
            'btc_trend':     state['btc_trend'],
            'breadth':       state['breadth'],
            'breadth_ratio': state['breadth_ratio'],
            'volatility':    state['volatility'],
            'risk_off':      1 if state['risk_off'] else 0,
        })
        subprocess.run(
            ['clickhouse-client', '--query',
             'INSERT INTO default.market_state_log FORMAT JSONEachRow'],
            input=row, text=True, timeout=5, capture_output=True
        )
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
