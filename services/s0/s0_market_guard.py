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
from s0 import core as s0_core
from s0 import ports as s0_ports
from s0 import adapters as s0_adapters


def _s0_market_data():
    """P6-04 晚绑定 factory：每次调用解析当前 `_rget`/`fapi_get`（保留
    monkeypatch seam）；输入侧 Port 由 callable 注入。"""
    return s0_adapters.S0RedisMarketAdapter(_rget, fapi_get)


def _s0_breadth_pool():
    """P6-04 晚绑定 state factory：包装 legacy `_breadth_symbols_cache`
    /`_breadth_symbols_ts`（dict-get/get/put 语义；重启用-backed 新 fixture）。"""
    return s0_adapters.BreadthPoolMemoryState(
        backing_get=lambda: (_breadth_symbols_cache, _breadth_symbols_ts),
        backing_put=_set_breadth_pool,
    )


def _set_breadth_pool(symbols, ts):
    global _breadth_symbols_cache, _breadth_symbols_ts
    _breadth_symbols_cache = symbols
    _breadth_symbols_ts = ts

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
    _pool = _s0_breadth_pool()
    _symbols, _ts = _pool.get()
    if _symbols and time.time() - _breadth_symbols_ts < 6 * 3600:
        return _breadth_symbols_cache
    try:
        tickers = _s0_market_data().fetch_ticker_24h()
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
    """从 s3 的 market:s3_data 读取指定币种时间窗（P6-04：经 Input Port）"""
    return _s0_market_data().read_s3_window(symbol, tf)


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


def sample_sentiment() -> dict:
    """读取 sentiment_bridge 采集的情绪数据（恐慌贪婪 + 资金费率聚合）
    （P6-04：经 Input Port；缺 key/畸形 → {}）。"""
    return _s0_market_data().read_sentiment()


def compute_state(btc_trend, volatility, amp, btc_below_ema60, atr_expanding,
                  breadth, breadth_ratio):
    """compute_state（P6-02）：分类主体委托 s0.core.classify_regime；
    sentiment/alts_sync/shock_score 的 wall-clock 采样与 IO 留在壳内，
    core 只消费已 gate 值（S0-3 保持：off-window 字段恒 0；version/timestamp
    由本函数注入——S0-1 fail-open / S0-9 三写语义零变化）。"""
    now_int = int(time.time())
    # ── sentiment 读取（IO，留壳） ──
    sent = sample_sentiment()

    # ── wall-clock 门（S0-3 Gate 保留在同处 orchestration shell） ──
    alts_sync_val = 0.0
    shock_val = 0
    if t_gate := int(time.time()) % 1800 < 30:
        sync, _f, _t2 = sample_alts_sync()
        alts_sync_val = sync
    if int(time.time()) % 60 < 30:
        shock_val = sample_shock_score()

    core = s0_core.classify_regime(
        btc_trend, volatility, amp, btc_below_ema60, atr_expanding,
        breadth, breadth_ratio,
        alts_sync_val=alts_sync_val, shock_val=shock_val,
        sentiment=sent,
    )
    # 字段序保持：version/timestamp 在最前（原 dict 字面量序）
    new_state = {
        "version":       VERSION,
        "timestamp":     now_int,
        **core,
    }
    return new_state


def _s0_publisher():
    """P6-03 晚绑定 factory：每次 write_state 解析当前模块态
    （`_rset` / `STATE_FILE` / shared.clickhouse_client.insert 均可在调用时被
    monkeypatch —— 测试语义保持；S0-9 三写失败语义经 adapter 逐字镜像）。"""
    from shared.clickhouse_client import insert as _ch_insert  # call-time 解析
    return s0_adapters.S0PublisherAdapter(
        redis_set=_rset,
        file_write=lambda state: s0_adapters.S0PublisherAdapter.atomic_file_write(
            state_file_path=str(STATE_FILE), state=state),
        ch_insert=_ch_insert,
        on_ch_error=lambda e: log.warning(f"CH write error: {e}"),
    )


def write_state(state):
    """S0 三写链（P6-03：delegate publisher）；public 签名/异常语义不变。"""
    return _s0_publisher().publish_state(state)


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
