"""
Redis 统一存储 — 替代所有 JSON 文件 I/O
所有数据存为 JSON 字符串，key 模式: trade:{category}:{name}

Redis 不可用时自动降级为文件 I/O，每 30s 重试连接。
"""

import json, time, logging
from pathlib import Path
import redis as _redis

_REDIS = None
_BASE = Path(__file__).resolve().parent.parent.parent

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
    """根据 key 获取对应的 JSON 文件路径，返回 (path, rel_path) 或 None"""
    item = KEY_MAP.get(key)
    if item is None:
        return None
    rel_path = item if isinstance(item, str) else item[0] if isinstance(item, tuple) else None
    if rel_path:
        fp = _BASE / rel_path
        return (fp, rel_path) if fp.exists() else None
    return None

def _read_file(key: str):
    """从文件读取数据"""
    item = _get_file(key)
    if item:
        fp, _ = item
        try:
            return json.loads(fp.read_text())
        except Exception:
            return {}
    return {}

def _write_file(key: str, data: dict):
    """双写文件"""
    item = _get_file(key)
    if item:
        fp, _ = item
        try:
            fp.write_text(json.dumps(data, indent=2, default=str))
        except Exception as e:
            _logger.warning(f'[RedisFallback] 写文件 {key} 失败: {e}')

def _conn():
    global _REDIS
    if _REDIS is None:
        _REDIS = _redis.Redis(host='127.0.0.1', port=6379, db=0, decode_responses=True)
    return _REDIS

def migrate_all():
    """启动时调用：从 JSON 文件重建 Redis（JSON 始终是最新的，通过双写保障）"""
    if not _check_redis():
        print('[迁移] Redis 不可用，跳过迁移')
        return 0
    r = _conn()
    count = 0
    for key, rel_path in KEY_MAP.items():
        if rel_path is None:
            continue
        fp = _BASE / rel_path
        if fp.exists():
            data = fp.read_text()
            r.set(key, data)
            print(f'[迁移] {key} ← {rel_path} ({len(data)} bytes)')
            count += 1
    return count

def get(key: str) -> dict:
    """读取数据：优先 Redis，降级到文件"""
    if _check_redis():
        try:
            r = _conn()
            raw = r.get(key)
            if raw:
                return json.loads(raw)
            # Redis 无数据时自动从文件补位
            item = _get_file(key)
            if item:
                fp, _ = item
                if fp.exists():
                    data = json.loads(fp.read_text())
                    r.set(key, fp.read_text())
                    return data
        except Exception as e:
            _logger.warning(f'[RedisFallback] get({key}) Redis 失败: {e}，降级到文件')
            _REDIS_AVAILABLE = False  # 标记不可用，下次重试
    # Redis 不可用 → 文件降级
    return _read_file(key)

def set(key: str, data: dict, *, double_write: bool = True):
    """写入数据：优先 Redis，同时双写文件"""
    encoded = json.dumps(data, indent=2, default=str)
    if _check_redis():
        try:
            r = _conn()
            r.set(key, encoded)
        except Exception as e:
            _logger.warning(f'[RedisFallback] set({key}) Redis 失败: {e}')
            _REDIS_AVAILABLE = False
    # 始终双写文件（数据保障）
    if double_write:
        _write_file(key, data)

def delete(key: str):
    """删除数据：优先 Redis，同步删文件"""
    if _check_redis():
        try:
            r = _conn()
            r.delete(key)
        except Exception:
            _REDIS_AVAILABLE = False
    # 同时清文件
    item = _get_file(key)
    if item:
        fp, _ = item
        try:
            fp.write_text('{}')
        except Exception:
            pass

def exists(key: str) -> bool:
    """检查键是否存在"""
    if _check_redis():
        try:
            r = _conn()
            return r.exists(key) > 0
        except Exception:
            _REDIS_AVAILABLE = False
    # 降级到文件
    item = _get_file(key)
    return item is not None

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
