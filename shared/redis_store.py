"""
Redis 统一存储 — 替代所有 JSON 文件 I/O
所有数据存为 JSON 字符串，key 模式: trade:{category}:{name}

V2 禁止本地文件业务存储。Redis 不可用时显式报错，不从旧文件回填。
"""

import json, time, logging
import redis as _redis

_REDIS = None

_REDIS_AVAILABLE = None  # None=未检查, True=可用, False=不可用
_REDIS_RETRY_AFTER = 30  # 不可用时每 30s 重试一次
_REDIS_LAST_CHECK = 0

_logger = logging.getLogger('redis_store')

def _check_redis():
    """检查 Redis 是否可用，缓存结果，定期重试"""
    global _REDIS_AVAILABLE, _REDIS_LAST_CHECK, _REDIS
    now = time.time()
    # 首次或定期重试
    if _REDIS_AVAILABLE is None or (not _REDIS_AVAILABLE and now - _REDIS_LAST_CHECK > _REDIS_RETRY_AFTER):
        try:
            if _REDIS is None:
                _REDIS = _redis.Redis(host='127.0.0.1', port=6379, db=0, decode_responses=True)
            _REDIS.ping()
            _REDIS_AVAILABLE = True
        except Exception:
            _REDIS_AVAILABLE = False
            _REDIS = None  # 下次重试重建连接
        _REDIS_LAST_CHECK = now
    return _REDIS_AVAILABLE

# Key 映射:  key_name -> relative_file_path (relative to _BASE)
KEY_MAP = {
    # S3 事件/市场数据
    'event:s3':                'trading_engine/strategies/config/s3_events.json',
    'market:s3_data':          'trading_engine/strategies/config/s3_market_data.json',
    'cache:s3_rolling':        'trading_engine/strategies/config/s3_rolling_cache.json',
    'signal:s3_signals':       'trading_engine/strategies/config/s3_signals.json',
    'mover:s3_spot':           'trading_engine/strategies/config/s3_spot_movers.json',
    # 策略持仓状态
    'state:s6':                'trading_engine/strategies/config/S6_state.json',
    'state:s8':                'trading_engine/strategies/config/S8_state.json',
    'state:sandbox':           'trading_engine/strategies/config/sandbox_state.json',
    # S0 市场状态
    'market:s0':               'trading_engine/services/s0/market_state.json',
    # s6_auto_trader 状态
    'checkpoint:pnl':          'trading_engine/shared/config/pnl_checkpoint.json',
    'state:trader':            'trading_engine/shared/config/trader_state.json',
    'log:trade':               'trading_engine/shared/config/trade_history.json',
    'breaker:circuit':         'trading_engine/shared/config/circuit_breaker.json',
    # 冷却
    'cd:loss':                 'trading_engine/shared/config/loss_cooldowns.json',
    'cd:s8a_symbol':           'config/s8a_symbol_cd.json',
    'cd:s8b_symbol':           'config/s8b_symbol_cd.json',
    # S2 信号
    'signal:s2_latest':        'config/s2_latest_signal.json',
    'signal:s2_watchlist':     'config/s2_watchlist.json',
    'signal:s2j':              'config/s2j_signals.json',
    # 候选池
    'pool:candidate':          'trading_engine/shared/config/candidate_pool.json',
    # PM 状态 / S7 网格
    'pm:positions':            'trading_engine/shared/config/pm_state.json',
    'pm:paused':               'config/pm_paused.json',
    'state:grid':              'trading_engine/services/s7/config/grid_state.json',
    # 共享持仓
    'share:positions':         'config/s8_positions.json',
    # S8B 泵信号去重
    's8b:seen_pumps':          None,
}

def _get_file(key: str) -> tuple:
    """Retired compatibility seam: business files are never resolved."""
    return None

def _read_file(key: str):
    raise RuntimeError('V2 business file storage is disabled')

def _write_file(key: str, data: dict):
    raise RuntimeError('V2 business file storage is disabled')

def _conn():
    global _REDIS
    if _REDIS is None:
        _REDIS = _redis.Redis(host='127.0.0.1', port=6379, db=0, decode_responses=True)
    return _REDIS


def strict_get(key: str):
    """Read a dedicated authority key with no file fallback."""
    if not _check_redis():
        raise ConnectionError('Redis authority backend unavailable')
    return _conn().get(key)


def strict_eval(script: str, numkeys: int, *keys_and_args):
    """Execute an authority Lua command with no file fallback."""
    if not _check_redis():
        raise ConnectionError('Redis authority backend unavailable')
    return _conn().eval(script, numkeys, *keys_and_args)


def migrate_all():
    """No file adoption in the clean-start V2 architecture."""
    raise RuntimeError('V2 file migration is disabled; rebuild projections from PostgreSQL')

def get(key: str) -> dict:
    raw = strict_get(key)
    return {} if raw is None else json.loads(raw)

def set(key: str, data: dict, *, double_write: bool = True):
    """Legacy cache write only. double_write is ignored; never writes files."""
    if not _check_redis():
        raise ConnectionError('Redis backend unavailable')
    return _conn().set(key, json.dumps(data, default=str, allow_nan=False))

def delete(key: str):
    if not _check_redis():
        raise ConnectionError('Redis backend unavailable')
    return _conn().delete(key)

def exists(key: str) -> bool:
    if not _check_redis():
        raise ConnectionError('Redis backend unavailable')
    return _conn().exists(key) > 0

def keys(pattern: str = '*') -> list:
    """列出键（Redis 不可用时返回空）"""
    if _check_redis():
        try:
            r = _conn()
            return r.keys(pattern)
        except Exception:
            _REDIS_AVAILABLE = False
    return []


# ── 发布订阅（通知，不做文件降级；消费方丢失可回退轮询） ──
def publish(channel: str, message: str = '1') -> bool:
    """发布消息。失败静默返回 False，调用方自行忽略。"""
    if not _check_redis():
        return False
    try:
        _conn().publish(channel, message)
        return True
    except Exception:
        _REDIS_AVAILABLE = False
        return False


def subscribe(channel: str):
    """订阅频道，返回 redis pubsub 对象（可能为 None）。

    调用方用 pubsub.get_message(timeout=...) 阻塞等待，异常时回退普通轮询。
    """
    if not _check_redis():
        return None
    try:
        ps = _conn().pubsub()
        ps.subscribe(channel)
        return ps
    except Exception:
        _REDIS_AVAILABLE = False
        return None


# ── 分布式锁（仅 Redis，不做文件降级；多进程原子选主） ──
def lock_owner(key: str):
    """返回锁当前持有者标识（原始字符串），无锁返回 None"""
    if not _check_redis():
        return None
    try:
        return _conn().get(key)
    except Exception:
        return None

def lock_acquire(key: str, value: str, ttl: int = 45) -> bool:
    """SET NX EX 原子抢占锁。仅当无锁时成功，返回是否抢到。"""
    if not _check_redis():
        return False
    try:
        return bool(_conn().set(key, value, nx=True, ex=ttl))
    except Exception:
        return False

def lock_renew(key: str, value: str, ttl: int = 45) -> bool:
    """仅当锁仍属于自己时续期（Lua 原子比较）。"""
    if not _check_redis():
        return False
    try:
        r = _conn()
        lua = ("if redis.call('get', KEYS[1]) == ARGV[1] "
               "then return redis.call('pexpire', KEYS[1], ARGV[2]) "
               "else return 0 end")
        return bool(r.eval(lua, 1, key, value, ttl * 1000))
    except Exception:
        return False


def lock_release(key: str, value: str) -> bool:
    """Release a lock only when it still belongs to value."""
    if not _check_redis():
        return False
    try:
        lua = ("if redis.call('get', KEYS[1]) == ARGV[1] "
               "then return redis.call('del', KEYS[1]) else return 0 end")
        return bool(_conn().eval(lua, 1, key, value))
    except Exception:
        return False
