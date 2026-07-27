"""
data_cache.py — Redis 共享数据缓存
供 S6/S8A/S8B 替代直接 API 调用，减少重复请求。

依赖:
- Redis key scanner:ticker_24h (由S9写入)
- Redis key scanner:exchange_info (本模块维护)
- Redis key scanner:klines:{interval}:{symbol} (本模块维护)
"""
import json, time
from pathlib import Path
import requests as _req

FAPI = 'https://fapi.binance.com'

def get_ticker_24h():
    """从 Redis 读取全量行情（S9写入）"""
    from shared.redis_store import get
    return get('scanner:ticker_24h')

def get_top_symbols(n=200) -> list:
    """从 Redis 读取排行列表"""
    from shared.redis_store import get
    data = get('scanner:ticker_24h')
    if not data or not data.get('data'):
        return []
    sorted_syms = sorted(data['data'].items(), key=lambda x: float(x[1].get('vol', 0)), reverse=True)
    return [s[0] for s in sorted_syms[:n]]

def get_exchange_info():
    """exchangeInfo 缓存1h"""
    from shared.redis_store import get, set
    data = get('scanner:exchange_info')
    if data and time.time() - data.get('ts', 0) < 3600:
        return data.get('data', {})
    # 缓存过期，拉API
    try:
        r = _req.get(f'{FAPI}/fapi/v1/exchangeInfo', timeout=10)
        info = r.json()
        set('scanner:exchange_info', {'ts': time.time(), 'data': info})
        return info
    except Exception:
        return data.get('data', {}) if data else {}

def get_symbol_info(symbol: str) -> tuple:
    """获取精度 (qty_precision, price_precision)"""
    info = get_exchange_info()
    for s in info.get('symbols', []):
        if s['symbol'] == symbol:
            return (s.get('quantityPrecision', 5), s.get('pricePrecision', 5))
    return (5, 5)

def get_klines(symbol: str, interval: str = '1h', limit: int = 51) -> list:
    """带缓存的 klines，过期自动重拉。间隔相同的数据共享缓存。"""
    from shared.redis_store import get, set
    # 用最大limit作为key，避免不同limit用同一缓存
    cache_key = f'scanner:klines:{interval}:{symbol}'
    data = get(cache_key)
    if data and time.time() - data.get('ts', 0) < 15:  # 15s 缓存
        cached = data.get('data', [])
        if len(cached) >= limit:
            return cached[-limit:]
    # 缓存过期或不足，拉API
    try:
        r = _req.get(f'{FAPI}/fapi/v1/klines', params={
            'symbol': symbol, 'interval': interval, 'limit': max(limit, 51)
        }, timeout=10)
        klines = r.json()
        if isinstance(klines, list) and len(klines) > 0:
            set(cache_key, {'ts': time.time(), 'data': klines})
            return klines[-limit:]
    except Exception:
        if data:
            return data.get('data', [])[-limit:]
        return []
    return []
