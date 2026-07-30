#!/usr/bin/env python3
"""
s7_core.py - 基础设施层（不可reload）
配置、API、日志、WebSocket行情Feed、状态读写
"""
import os, json, time, requests, hmac, hashlib, threading, subprocess, websocket, sys
from collections import deque
from pathlib import Path
from urllib.parse import urlencode
from datetime import datetime

# === 配置 ===
CONFIG_FILE = Path(__file__).parent / 'config/grid.env'
LOG_DIR     = Path(__file__).parent.parent.parent / 'logs/s7'

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from shared.redis_store import get as _rget, set as _rset

def load_config():
    cfg = {}
    if CONFIG_FILE.exists():
        for line in CONFIG_FILE.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                cfg[k.strip()] = v.strip()
    return cfg

CFG = load_config()
API_KEY    = CFG.get('GRID_API_KEY', '')
API_SECRET = CFG.get('GRID_API_SECRET', '')
TG_TOKEN   = CFG.get('GRID_TG_TOKEN', '')
TG_CHAT    = CFG.get('GRID_TG_CHAT', '')
FAPI       = CFG.get('GRID_FAPI', 'https://fapi.binance.com')

GRID_SYMBOLS    = CFG.get('GRID_SYMBOLS', 'ETHUSDT,SOLUSDT').split(',')
TOTAL_CAPITAL   = float(CFG.get('GRID_CAPITAL', '500'))
GRID_LAYERS     = int(CFG.get('GRID_LAYERS', '5'))      # 单侧层数
ATR_MULTIPLIER  = float(CFG.get('GRID_ATR_MULT', '0.5'))
MAX_INV_RATIO   = float(CFG.get('GRID_MAX_INV', '0.3')) # 最大库存比例

# === 工具 ===
def log(msg):
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    print(line, flush=True)
    try:
        LOG_DIR.mkdir(exist_ok=True)
        (LOG_DIR / f'{datetime.now().strftime("%Y%m%d")}.log').open('a').write(line + '\n')
    except Exception:
        pass

def tg(msg):
    if not TG_TOKEN or not TG_CHAT:
        return
    try:
        requests.post(f'https://api.telegram.org/bot{TG_TOKEN}/sendMessage',
            json={'chat_id': TG_CHAT, 'text': msg, 'parse_mode': 'Markdown'}, timeout=10)
    except:
        pass

def sign(params):
    p = dict(params)
    p['timestamp'] = int(time.time() * 1000)
    query = urlencode(p)
    p['signature'] = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    return p

def _sign_and_send_post(path, params):
    p = dict(params)
    p['timestamp'] = int(time.time() * 1000)
    qs = urlencode(p)
    sig = hmac.new(API_SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    qs_final = qs + '&signature=' + sig
    r = requests.post(f'{FAPI}{path}', data=qs_final,
        headers={'X-MBX-APIKEY': API_KEY, 'Content-Type': 'application/x-www-form-urlencoded'},
        timeout=10)
    return r.json()

def fapi_get(path, params=None):
    p = sign(params or {})
    r = requests.get(f'{FAPI}{path}', params=p,
        headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
    return r.json()

def fapi_post(path, params):
    return _sign_and_send_post(path, params)

def fapi_delete(path, params):
    p = sign(params)
    r = requests.delete(f'{FAPI}{path}', params=p,
        headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
    return r.json()

# === 实时行情 Feed（WebSocket bookTicker）===
_feed: dict = {}

class MarketDataFeed:
    """单条 WS combined stream 订阅所有 symbol 的 bookTicker，毫秒级更新"""
    def __init__(self, symbols: list):
        self.symbols = symbols
        self._ws = None
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        streams = '/'.join(f"{s.lower()}@bookTicker" for s in self.symbols)
        url = f"wss://fstream.binance.com/stream?streams={streams}"
        while True:
            try:
                ws = websocket.WebSocketApp(url,
                    on_message=self._on_message,
                    on_error=lambda ws, e: log(f'[Feed] WS error: {e}'))
                ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                log(f'[Feed] reconnect: {e}')
            time.sleep(3)

    def _on_message(self, ws, message):
        try:
            d = json.loads(message).get('data', {})
            sym = d.get('s', '')
            if sym:
                _feed[sym] = {
                    'bid': float(d['b']), 'ask': float(d['a']),
                    'bid_qty': float(d['B']), 'ask_qty': float(d['A']),
                    'ts': time.time(),
                }
        except Exception:
            pass

_mdf: MarketDataFeed = None

# === 市场数据函数 ===
def get_price(symbol):
    snap = _feed.get(symbol)
    if snap:
        return (snap['bid'] + snap['ask']) / 2
    r = requests.get(f'{FAPI}/fapi/v1/ticker/price',
        params={'symbol': symbol}, timeout=5).json()
    return float(r['price'])

def get_imbalance(symbol) -> float:
    snap = _feed.get(symbol)
    if not snap:
        return 0.5
    total = snap['bid_qty'] + snap['ask_qty']
    return snap['bid_qty'] / total if total > 0 else 0.5

def get_klines(symbol, interval='1h', limit=60):
    r = requests.get(f'{FAPI}/fapi/v1/klines',
        params={'symbol': symbol, 'interval': interval, 'limit': limit}, timeout=10)
    return r.json()

def get_atr(symbol, period=14):
    klines = get_klines(symbol, '1h', period + 1)
    trs = []
    for i in range(1, len(klines)):
        h, l, pc = float(klines[i][2]), float(klines[i][3]), float(klines[i-1][4])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs) / len(trs) if trs else 0

def get_ema(symbol, period=20, interval='1h'):
    klines = get_klines(symbol, interval, period * 2)
    closes = [float(k[4]) for k in klines]
    if len(closes) < period:
        return 0
    k = 2 / (period + 1)
    ema = sum(closes[:period]) / period
    for c in closes[period:]:
        ema = c * k + ema * (1 - k)
    return ema

def get_symbol_info(symbol):
    r = requests.get(f'{FAPI}/fapi/v1/exchangeInfo', timeout=10).json()
    qty_prec, price_prec = 3, 2
    for s in r['symbols']:
        if s['symbol'] != symbol:
            continue
        for f in s['filters']:
            if f['filterType'] == 'LOT_SIZE':
                step = f['stepSize']
                qty_prec = len(step.rstrip('0').split('.')[-1]) if '.' in step else 0
            if f['filterType'] == 'PRICE_FILTER':
                tick = f['tickSize']
                price_prec = len(tick.rstrip('0').split('.')[-1]) if '.' in tick else 0
    return qty_prec, price_prec

def get_balance():
    r = fapi_get('/fapi/v2/account')
    for a in r.get('assets', []):
        if a['asset'] == 'USDT':
            return float(a['availableBalance'])
    return 0

# === 订单管理（核心层，供 MarketGuard 和 logic 共用）===
def get_open_orders(symbol):
    return fapi_get('/fapi/v1/openOrders', {'symbol': symbol})

def get_inventory(symbol):
    r = fapi_get('/fapi/v2/positionRisk', {'symbol': symbol})
    if isinstance(r, list) and r:
        return float(r[0].get('positionAmt', 0))
    return 0

def cancel_all_orders(symbol):
    orders = get_open_orders(symbol)
    if not orders:
        return
    r = fapi_delete('/fapi/v1/allOpenOrders', {'symbol': symbol})
    log(f'[撤单] {symbol} 撤销所有挂单')
    return r

def market_close(symbol, qty):
    qty_prec, _ = get_symbol_info(symbol)
    qty = round(abs(qty), qty_prec)
    if qty <= 0:
        return {}
    return fapi_post('/fapi/v1/order', {
        'symbol': symbol, 'side': 'SELL', 'type': 'MARKET',
        'quantity': qty, 'positionSide': 'BOTH', 'reduceOnly': 'true',
    })

# === S2 Shock Filter ===
def get_s2_shock_score(symbol):
    """返回 (symbol_shock, market_heat)"""
    try:
        data = _rget('signal:s2_latest')
        if not data:
            return 0, 0
        signals = data.get('signals', [])
        market_heat = len(signals)
        symbol_shock = sum(1 for s in signals if s.get('symbol') == symbol)
        return symbol_shock, market_heat
    except:
        return 0, 0

# === 市场守护线程 MarketGuard ===
class MarketGuard:
    """
    每30秒采样，维护5分钟滑动窗口，从4个维度判断下跌类型：
    - crash  : 真暴跌（速度快+联动+放量）→ 全平
    - bear   : 阴跌（慢速持续）          → 停买
    - pullback: 回撤（短暂快速）          → 观察
    - normal : 正常                       → 不干预
    """
    SAMPLE_INTERVAL = 30   # 秒
    WINDOW_SIZE     = 10   # 5分钟 = 10个采样点

    def __init__(self, symbols):
        self.symbols  = symbols
        self.samples  = {s: deque(maxlen=self.WINDOW_SIZE) for s in symbols}
        self.verdict  = {s: 'normal' for s in symbols}   # 当前结论
        self.lock     = threading.Lock()
        self._thread  = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        log('[Guard] 市场守护线程已启动')

    def get_verdict(self, symbol):
        with self.lock:
            return self.verdict.get(symbol, 'normal')

    def _run(self):
        while True:
            try:
                self._sample_all()
                self._judge_all()
            except Exception as e:
                log(f'[Guard] 异常: {e}')
            time.sleep(self.SAMPLE_INTERVAL)

    def _sample_all(self):
        ts = time.time()
        for sym in self.symbols:
            try:
                price = get_price(sym)
                klines_1m = get_klines(sym, '1m', 3)
                vol = float(klines_1m[-1][5]) if klines_1m else 0
                with self.lock:
                    self.samples[sym].append({'ts': ts, 'price': price, 'vol': vol})
            except:
                pass

    def _velocity(self, sym):
        with self.lock:
            pts = list(self.samples.get(sym, []))
        if len(pts) < 2:
            return 0
        return (pts[-1]['price'] - pts[0]['price']) / pts[0]['price'] if pts[0]['price'] > 0 else 0

    def _judge_all(self):
        btc_vel = self._velocity('BTCUSDT') if 'BTCUSDT' in self.symbols else 0

        for sym in self.symbols:
            verdict = self._judge_one(sym, btc_vel)
            with self.lock:
                old = self.verdict[sym]
                self.verdict[sym] = verdict
                if old != verdict:
                    log(f'[Guard] {sym} 判断变更: {old} → {verdict}')
                    tg(f'🛡 *Guard* `{sym}` {old} → *{verdict}*')
            if verdict == 'crash' and old != 'crash':
                log(f'[Guard] 🚨 {sym} 检测到crash，立即紧急平仓所有标的')
                for sym2 in self.symbols:
                    try:
                        cancel_all_orders(sym2)
                        inv = get_inventory(sym2)
                        if abs(inv) > 0:
                            market_close(sym2, abs(inv))
                    except Exception as e:
                        log(f'[Guard] 紧急平仓 {sym2} 失败: {e}')
                tg('🚨 MarketGuard 紧急平仓已执行')

    def _judge_one(self, sym, btc_vel):
        with self.lock:
            pts = list(self.samples[sym])
        if len(pts) < 3:
            return 'normal'

        velocity = (pts[-1]['price'] - pts[0]['price']) / pts[0]['price'] if pts[0]['price'] > 0 else 0

        drops = sum(1 for i in range(1, len(pts)) if pts[i]['price'] < pts[i-1]['price'])
        duration_ratio = drops / (len(pts) - 1)

        vols = [p['vol'] for p in pts if p['vol'] > 0]
        if len(vols) >= 4:
            recent_vol = sum(vols[-3:]) / 3
            base_vol   = sum(vols[:-3]) / len(vols[:-3])
            vol_ratio  = recent_vol / base_vol if base_vol > 0 else 1.0
        else:
            vol_ratio = 1.0

        correlated = (velocity < -0.005 and btc_vel < -0.005) or sym == 'BTCUSDT'

        if velocity < -0.03 and vol_ratio > 1.5 and correlated:
            return 'crash'
        if velocity < -0.015 and duration_ratio > 0.7:
            return 'bear'
        if velocity < -0.02 and duration_ratio < 0.5:
            return 'pullback'
        if velocity < -0.01 and correlated:
            return 'bear'
        return 'normal'

# === 状态持久化 ===
_STATE_KEY = 'state:grid'

def load_state():
    try:
        state = _rget(_STATE_KEY)
        if state:
            return state
    except:
        pass
    return {'grids': {}, 'realized_pnl': 0.0}

def save_state(state):
    _rset(_STATE_KEY, state)

# === 全局变量（由 runner 初始化）===
_guard = None  # MarketGuard 实例
