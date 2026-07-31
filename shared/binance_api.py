"""
Binance Futures API — 统一签名请求 + 沙盘拦截
供 s6_auto_trader / shared_executor / position_manager 等模块使用。
"""
import hmac, hashlib, time, requests, importlib, sys
from urllib.parse import urlencode
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
_CONFIG_FILE = _BASE / 'config/binance.env'
sys.path.insert(0, str(Path(__file__).resolve().parent))
def load_config():
    env = {}
    with open(_CONFIG_FILE) as f:
        for line in f:
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env

CFG = load_config()
API_KEY = CFG['BINANCE_API_KEY']
SECRET = CFG['BINANCE_API_SECRET']
TG_TOKEN = CFG['TG_NOTIFY_TOKEN']
TG_CHAT_ID = int(CFG['TG_NOTIFY_CHAT_ID'])

# Testnet 模式（binance.env 中设 BINANCE_TESTNET=true 自动切换）
_IS_TESTNET = CFG.get('BINANCE_TESTNET', '').lower() == 'true'
FAPI = 'https://demo-fapi.binance.com' if _IS_TESTNET else 'https://fapi.binance.com'
FSTREAM = 'wss://demo-fstream.binance.com' if _IS_TESTNET else 'wss://fstream.binance.com'

# Testnet 模式使用独立的 API Key
if _IS_TESTNET:
    _TN_KEY = CFG.get('BINANCE_TESTNET_API_KEY', '')
    _TN_SEC = CFG.get('BINANCE_TESTNET_API_SECRET', '')
    if _TN_KEY and _TN_SEC:
        API_KEY = _TN_KEY
        SECRET = _TN_SEC


def sign(params: dict) -> dict:
    params['timestamp'] = int(time.time() * 1000)
    qs = urlencode(params)
    params['signature'] = hmac.new(SECRET.encode(), qs.encode(), hashlib.sha256).hexdigest()
    return params


def _sandbox_intercept(path: str, params: dict):
    try:
        sb = importlib.import_module('scripts.sandbox')
        if not sb.is_active():
            return None
        low = path.lower().replace('-', '').replace('_', '')
        if 'positionrisk' in low:
            return sb.mock_get_position_risk((params or {}).get('symbol'))
        if 'account' in low and 'trade' not in low:
            return sb.mock_get_account()
        if 'openorders' in low:
            return sb.mock_get_open_orders((params or {}).get('symbol'))
        if 'order' in low or 'algo' in low:
            if 'cancel' in low or 'delete' in low:
                return sb.mock_cancel_order((params or {}).get('symbol', ''), (params or {}).get('orderId'))
            return sb.mock_post_order(params or {})
    except Exception:
        pass
    return None


import health

def fapi_get(path: str, params: dict = None):
    sb = _sandbox_intercept(path, params or {})
    if sb is not None:
        return sb
    t0 = time.time()
    try:
        p = sign(params or {})
        r = requests.get(f'{FAPI}{path}', params=p, headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
        data = r.json()
        health.record('fapi_get', success=True, latency_ms=(time.time()-t0)*1000)
        return data
    except Exception as e:
        health.record('fapi_get', success=False, latency_ms=(time.time()-t0)*1000)
        raise


def fapi_post(path: str, params: dict):
    sb = _sandbox_intercept(path, params)
    if sb is not None:
        return sb
    t0 = time.time()
    try:
        p = sign(params)
        r = requests.post(f'{FAPI}{path}', params=p, headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
        data = r.json()
        health.record('fapi_post', success=True, latency_ms=(time.time()-t0)*1000)
        return data
    except Exception as e:
        health.record('fapi_post', success=False, latency_ms=(time.time()-t0)*1000)
        raise


def fapi_delete(path: str, params: dict):
    sb = _sandbox_intercept(path, params)
    if sb is not None:
        return sb
    t0 = time.time()
    try:
        p = sign(params)
        r = requests.delete(f'{FAPI}{path}', params=p, headers={'X-MBX-APIKEY': API_KEY}, timeout=10)
        data = r.json()
        health.record('fapi_delete', success=True, latency_ms=(time.time()-t0)*1000)
        return data
    except Exception as e:
        health.record('fapi_delete', success=False, latency_ms=(time.time()-t0)*1000)
        raise


def fapi_get_public(path: str, params: dict = None):
    if params is None:
        params = {}
    try:
        r = requests.get(f'{FAPI}{path}', params=params, timeout=10)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None
