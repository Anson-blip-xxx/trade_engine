"""
event_bus.py — 模块间事件总线
用 Redis 做消息脊梁，实现 S8A/S8B/S6/S2 之间信息串联。

事件类型:
  rejection   信号被拒绝（带拒绝原因）→ 其他模块可反向利用
  signal      交易信号（方向/评分/理由）→ 消费方按方向订阅
  position    开/平仓事件 → PM 发布，各系统做状态同步

使用方式:
  from shared.event_bus import publish, poll, subscribe, pause, resume, is_paused

  # S8A 拒绝了一个信号 → S6 可读作多头候选
  publish('rejection', symbol='CLOUSDT', source='S8A', reason='15min趋势多头')

  # S6 消费拒绝事件
  for ev in poll('rejection', source='S8A', since=ts):
      if '趋势多头' in ev['reason']:
          add_as_long_candidate(ev['symbol'])

  # 全局暂停/恢复（所有系统共用一把锁）
  pause('重构事件总线')
  resume()
  is_paused()  # → True/False
"""

import time, json
from pathlib import Path
from shared.redis_store import get, set as _rset, delete as _rdel

# ============================================================
# 事件键模式: event:{type}:{symbol}          ← 事件本体
#            event:index:{type}             ← 时间排序索引（列表）
#            event:consumer:{name}:cursor   ← 消费者游标（时间戳）
# ============================================================

EVENT_TTL = 7200         # 事件保留 2h 自动过期（靠索引裁剪+惰性清理）
INDEX_MAX = 500          # 每类事件索引上限
_PAUSE_KEY = 'system:pause_new_pos'
_DEFAULT_CURSOR = 'event:consumer:default:cursor'


# ─── 发布 ───────────────────────────────────────────────────

def publish(event_type: str, *, symbol: str, source: str,
            **extra) -> None:
    """发布一条事件。event_type: rejection / signal / position"""
    now = time.time()
    payload = {
        'ts': now,
        'symbol': symbol.upper(),
        'source': source,
        'event_type': event_type,
        **extra,
    }
    key = f'event:{event_type}:{symbol.upper()}'
    _rset(key, payload)

    # 维护索引（时间倒序）
    idx_key = f'event:index:{event_type}'
    index = get(idx_key) or []
    index = [(s, t) for s, t in index if s != symbol.upper()]
    index.insert(0, (symbol.upper(), now))
    if len(index) > INDEX_MAX:
        # 裁剪尾部 + 清理对应 key
        for rem_sym, _ in index[INDEX_MAX:]:
            _rdel(f'event:{event_type}:{rem_sym}')
    _rset(idx_key, index[:INDEX_MAX])


# ─── 轮询 ───────────────────────────────────────────────────

def poll(event_type: str, *, source: str = None,
         since: float = None, limit: int = 20) -> list:
    """轮询获取某类事件。since=None 时使用消费者游标。"""
    if since is None:
        since = _get_cursor(event_type, source)

    idx_key = f'event:index:{event_type}'
    index = get(idx_key) or []

    results = []
    for sym, ts in index:
        if ts < since:
            break
        ev = get(f'event:{event_type}:{sym}')
        if not ev:
            continue
        if source and ev.get('source') != source:
            continue
        results.append(ev)
        if len(results) >= limit:
            break

    # 更新游标
    if results:
        _set_cursor(event_type, source, results[0]['ts'])

    return results


def subscribe(event_type: str, *, source: str = None) -> list:
    """subscribe 是 poll 的别名，语义更清晰"""
    return poll(event_type, source=source)


# ─── 游标管理（消费者进度） ────────────────────────────────────

def _cursor_key(event_type: str, source: str = None) -> str:
    parts = ['event:cursor', event_type]
    if source:
        parts.append(source)
    return ':'.join(parts)


def _get_cursor(event_type: str, source: str = None) -> float:
    data = get(_cursor_key(event_type, source))
    if isinstance(data, dict):
        return data.get('ts', 0.0)
    return 0.0


def _set_cursor(event_type: str, source: str, ts: float):
    _rset(_cursor_key(event_type, source), {'ts': ts})


# ─── 全局暂停/恢复 ────────────────────────────────────────────

def pause(reason: str = '维护中') -> None:
    """暂停所有系统开新仓"""
    _rset(_PAUSE_KEY, {'paused': True, 'ts': time.time(), 'reason': reason})


def resume() -> None:
    """恢复开仓"""
    _rset(_PAUSE_KEY, {'paused': False})


def is_paused() -> bool:
    result = get(_PAUSE_KEY)
    if isinstance(result, dict):
        return result.get('paused', False)
    return False


def pause_info() -> dict:
    return get(_PAUSE_KEY) or {'paused': False}


# ─── 管理 ────────────────────────────────────────────────────

def clear(event_type: str = None, symbol: str = None) -> None:
    """清理事件。event_type=None → 清空所有"""
    if event_type:
        if symbol:
            _rdel(f'event:{event_type}:{symbol.upper()}')
        else:
            index = get(f'event:index:{event_type}') or []
            for sym, _ in index:
                _rdel(f'event:{event_type}:{sym}')
            _rdel(f'event:index:{event_type}')
    else:
        # 扫 key 清理（慎用）
        for key in keys('event:*'):
            _rdel(key)


def stats() -> dict:
    """事件总线状态"""
    result = {}
    for suffix in ('rejection', 'signal', 'position'):
        index = get(f'event:index:{suffix}') or []
        result[suffix] = {'count': len(index)}
        if index:
            result[suffix]['latest_ts'] = index[0][1]
            result[suffix]['latest_symbol'] = index[0][0]
    return result
